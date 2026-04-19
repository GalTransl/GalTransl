"""LLM 翻译前端。

该模块把项目配置转化为一轮完整的翻译流水线：
1. 读取输入文件 → 通过文件插件解析为 trans_list
2. 按 splitter 切成多个 chunk，按 name/size 排序
3. 载入字典 / name 替换表 / 初始化后端 gptapi
4. 启动 worker 协程池（带信号量 + 自适应并发调节）消费 chunk 队列
5. 每个 chunk：前处理 → 读缓存命中判定 → 调 gptapi.batch_translate →（可选）校对 → 后处理
6. 文件全部 chunk 完成后：find_problems + 写完整快照缓存(post_save) + 合并输出 + 通过文件插件保存

注：启动时不再做全局 jsonl 合并，仅在单文件完成时通过 `save_transCache_to_json(..., post_save=True)`
重写快照并清理 append 日志。
"""

from typing import List, Dict, Any, Optional, Union, Tuple
from os import makedirs, cpu_count, sep as os_sep,listdir
from os.path import join as joinpath, exists as isPathExists, dirname
from venv import logger
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import time
import asyncio
from dataclasses import dataclass

from GalTransl import LOGGER
from GalTransl.i18n import get_text, GT_LANG
from GalTransl.Cache import get_transCache_from_json
from GalTransl.ConfigHelper import initDictList, CProjectConfig
from GalTransl.Dictionary import CGptDict, CNormalDic
from GalTransl.Problem import find_problems
from GalTransl.Cache import save_transCache_to_json
from GalTransl.Name import load_name_table, dump_name_table_from_chunks
from GalTransl.CSerialize import update_json_with_transList, save_json
from GalTransl.Dictionary import CNormalDic, CGptDict
from GalTransl.ConfigHelper import CProjectConfig, initDictList
from GalTransl.Utils import get_file_list
from GalTransl.CSplitter import (
    SplitChunkMetadata,
    DictionaryCombiner,
)
from GalTransl.TerminalOutput import should_print_translation_logs, terminal_progress


def _runtime_project_dir(projectConfig: CProjectConfig) -> str:
    """取当前运行时使用的项目目录（桌面端/服务端会覆盖为实际工作目录）。"""
    return getattr(projectConfig, "runtime_project_dir", projectConfig.getProjectDir())


def _update_runtime(projectConfig: CProjectConfig, **kwargs):
    """向 server 运行时状态上报进度信息（桌面端订阅用）。

    服务端未启动时静默失败，不影响 CLI 运行。
    """
    try:
        from GalTransl.server import update_runtime_status
        update_runtime_status(_runtime_project_dir(projectConfig), **kwargs)
    except Exception:
        return


@dataclass
class AdaptiveWorkerState:
    """自适应并发状态。

    - max_workers: 用户在配置中指定的并发上限，运行期间不变。
    - effective_workers: 当前实际允许的并发数，会被 auto_tune_workers 动态调整。
    """
    max_workers: int
    effective_workers: int


async def auto_tune_workers(
    projectConfig: CProjectConfig,
    adaptive_state: AdaptiveWorkerState,
    apply_limit,
):
    """后台自适应并发调节任务。

    基于最近 30s 的请求健康度（429 比例 / 平均延迟）上下调 effective_workers：
    - 429 比例高 或 延迟高 → 减 1（最低 1）
    - 两者都低 → 加 1（不超过 max_workers）
    通过 apply_limit 回调去 acquire/release 信号量槽位，实现软限流。
    """
    metrics = getattr(projectConfig, "request_health_metrics", None)
    if metrics is None:
        return

    while True:
        await asyncio.sleep(3.0)
        snapshot = metrics.snapshot(window_seconds=30.0)
        total = int(snapshot.get("total", 0))
        if total < 8:
            # 样本不足，避免噪声触发调整
            continue

        ratio_429 = float(snapshot.get("rate_limited_ratio", 0.0))
        avg_latency = float(snapshot.get("avg_latency", 0.0))
        current = adaptive_state.effective_workers
        target = current

        if ratio_429 >= 0.18 or avg_latency >= 12.0:
            target = max(1, current - 1)
        elif ratio_429 <= 0.05 and avg_latency <= 6.0:
            target = min(adaptive_state.max_workers, current + 1)

        if target != current:
            await apply_limit(target)


def _check_stop_requested(projectConfig: CProjectConfig):
    """协作式取消检查点：若桌面端/服务端触发 stop_event，则抛出 JobCancelledError 中止当前任务。

    在各关键步骤（IO 前、进入循环、chunk 处理前等）调用，避免写到一半被硬中断。
    """
    stop_event = getattr(projectConfig, "stop_event", None)
    if stop_event is not None and stop_event.is_set():
        from GalTransl.Service import JobCancelledError

        raise JobCancelledError()


def _build_runtime_file_maps(ordered_chunks: list[SplitChunkMetadata], input_dir: str) -> tuple[dict[str, int], dict[str, str]]:
    """构造两个给前端使用的映射：

    - file_totals: {显示名: 该文件总行数}，用于前端展示每个文件的进度分母。
    - cache_file_display_map: {缓存文件名(.json): 显示名}，用于把缓存回写事件关联到对应文件。
    """
    file_totals: dict[str, int] = {}
    cache_file_display_map: dict[str, str] = {}

    for chunk in ordered_chunks:
        display_name = chunk.file_path.replace(input_dir, "").lstrip(os_sep).replace(os_sep, "/")
        file_totals.setdefault(display_name, 0)
        file_totals[display_name] += chunk.chunk_non_cross_size
        cache_key = display_name.replace("/", "-}")
        if chunk.total_chunks > 1:
            cache_key = f"{cache_key}_{chunk.chunk_index}"

        if not cache_key.endswith(".json"):
            cache_key = f"{cache_key}.json"
        cache_file_display_map[cache_key] = display_name

    return file_totals, cache_file_display_map


async def update_progress_title(
    bar, semaphore, workersPerProject: int, projectConfig: CProjectConfig
):
    """异步任务，用于动态更新 alive_bar 的标题以显示活动工作线程数。"""
    base_title = "翻译进度"
    is_interactive = should_print_translation_logs(projectConfig)
    while True:
        try:
            # 计算当前活动的任务数
            # semaphore.acquire() 会减少 _value，semaphore.release() 会增加 _value
            # 因此，活动任务数 = 总容量 - 当前可用容量
            reserved_workers = int(getattr(projectConfig, "runtime_workers_reserved", 0))
            active_workers = workersPerProject - semaphore._value - reserved_workers
            # 确保 active_workers 不会是负数（以防万一）
            active_workers = max(0, active_workers)
            configured_workers = int(
                getattr(projectConfig, "runtime_workers_effective", workersPerProject)
            )
            configured_workers = max(1, configured_workers)
            if active_workers == 0:
                projectConfig.active_workers = configured_workers
            else:
                projectConfig.active_workers = active_workers
            _update_runtime(
                projectConfig,
                workers_active=active_workers,
                workers_configured=configured_workers,
            )
            # 更新标题（仅 CLI 模式有 bar）
            if is_interactive:
                new_title = f"{base_title} [{active_workers}/{configured_workers} 并发]"
                bar.title(new_title)

            # 每隔一段时间更新一次，避免过于频繁
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            # 当任务被取消时，设置最终标题并退出循环
            if is_interactive:
                bar.title(f"{base_title} [处理完成]")
            break
        except Exception as e:
            # 记录任何其他异常并停止更新
            LOGGER.error(f"更新进度条标题时出错: {e}")
            bar.title(f"{base_title} [更新出错]")
            break


def preprocess_trans_list(trans_list, projectConfig, pre_dic, tPlugins=None):
    """翻译前处理：插件before_src → 对话分析 → 预处理字典替换源文 → 预处理字典替换说话人 → 插件after_src"""
    for tran in trans_list:
        if tPlugins:
            for plugin in tPlugins:
                try:
                    tran = plugin.plugin_object.before_src_processed(tran)
                except Exception as e:
                    LOGGER.error(
                        get_text("plugin_execution_failed", GT_LANG, plugin.name, e)
                    )

        if projectConfig.getFilePlugin() in [
            "file_galtransl_json",
            "file_mtbench_aio",
        ]:
            if projectConfig.select_translator not in ["ForNovel"]:
                tran.analyse_dialogue()

        tran.post_jp = pre_dic.do_replace(tran.post_jp, tran)

        if projectConfig.getDictCfgSection("usePreDictInName"):
            if isinstance(tran.speaker, str) and isinstance(tran._speaker, str):
                tran.speaker = pre_dic.do_replace(tran.speaker, tran)

        if tPlugins:
            for plugin in tPlugins:
                try:
                    tran = plugin.plugin_object.after_src_processed(tran)
                except Exception as e:
                    LOGGER.error(
                        get_text("plugin_execution_failed", GT_LANG, plugin.name, e)
                    )


def postprocess_trans_list(trans_list, projectConfig, post_dic, tPlugins=None):
    """翻译后处理：插件before_dst → 恢复对话符号 → 后处理字典替换译文 → 插件after_dst"""
    for tran in trans_list:
        if tPlugins:
            for plugin in tPlugins:
                try:
                    tran = plugin.plugin_object.before_dst_processed(tran)
                except Exception as e:
                    LOGGER.error(f" 插件 {plugin.name} 执行失败: {e}", exc_info=True)

        tran.recover_dialogue_symbol()
        tran.post_zh = post_dic.do_replace(tran.post_zh, tran)

        if tPlugins:
            for plugin in tPlugins:
                try:
                    tran = plugin.plugin_object.after_dst_processed(tran)
                except Exception as e:
                    LOGGER.error(
                        get_text("plugin_execution_failed", GT_LANG, plugin.name, e)
                    )


async def doLLMTranslate(
    projectConfig: CProjectConfig,
) -> bool:
    """整个项目的翻译入口。

    负责：准备目录/字典/插件/后端 → 载入文件并切块 → 启动 worker 协程池 →
    等所有 chunk 结束后清理自适应调节与进度条相关后台任务。
    单文件完成的后续工作（find_problems / 写缓存快照 / 合并输出）由 `postprocess_results` 触发。
    """

    _check_stop_requested(projectConfig)

    # ---- 1. 基础路径与配置项 ----
    project_dir = projectConfig.getProjectDir()
    input_dir = projectConfig.getInputPath()
    output_dir = projectConfig.getOutputPath()
    cache_dir = projectConfig.getCachePath()
    pre_dic_list = projectConfig.getDictCfgSection()["preDict"]
    post_dic_list = projectConfig.getDictCfgSection()["postDict"]
    gpt_dic_list = projectConfig.getDictCfgSection()["gpt.dict"]
    default_dic_dir = projectConfig.getDictCfgSection()["defaultDictFolder"]
    workersPerProject = projectConfig.getKey("workersPerProject") or 1
    semaphore = asyncio.Semaphore(workersPerProject)
    adaptive_state = AdaptiveWorkerState(
        max_workers=max(1, workersPerProject),
        effective_workers=max(1, workersPerProject),
    )
    projectConfig.runtime_workers_effective = adaptive_state.effective_workers
    projectConfig.runtime_workers_reserved = 0
    fPlugins = projectConfig.fPlugins       # 文件插件（负责 load/save 特定格式）
    tPlugins = projectConfig.tPlugins       # 文本插件（前/后处理钩子）
    eng_type = projectConfig.select_translator  # 选定的后端引擎标识
    input_splitter = projectConfig.input_splitter
    # 清空跨任务残留的"文件已完成 chunk"记录，避免二次运行时误判
    SplitChunkMetadata.clear_file_finished_chunk()
    total_chunks = []
    projectConfig.active_workers = 1
    _update_runtime(projectConfig, workers_active=0, workers_configured=adaptive_state.effective_workers)
    
    makedirs(output_dir, exist_ok=True)
    makedirs(cache_dir, exist_ok=True)

    _check_stop_requested(projectConfig)

    # 语言设置
    if val := projectConfig.getKey("language"):
        sp = val.split("2")
        projectConfig.source_lang = sp[0]
        projectConfig.target_lang = sp[-1]

    # 获取待翻译文件列表
    file_list = get_file_list(projectConfig.getInputPath())
    if not file_list:
        # dump-name / GenDic 等仅基于输入文件的短路流程，空目录不算致命错误，友好返回
        if "dump-name" in eng_type or eng_type == "GenDic":
            LOGGER.warning(
                f"{projectConfig.getInputPath()} 中没有待翻译的文件，已跳过。"
            )
            return True
        raise RuntimeError(f"{projectConfig.getInputPath()}中没有待翻译的文件")

    # 按文件名自然排序（处理数字部分）
    import re

    def natural_sort_key(s):
        return [
            int(text) if text.isdigit() else text.lower()
            for text in re.split(r"(\d+)", s)
        ]

    file_list.sort(key=natural_sort_key)

    all_jsons = []
    # ---- 2. 读取所有文件并切分为 chunk ----
    # 使用线程池并发读文件（IO 密集型），同时通过 fPlugins 解析为 json_list
    file_loader_workers = max(1, min(cpu_count() or 1, 8))
    with ThreadPoolExecutor(max_workers=file_loader_workers) as executor:
        future_to_file = {
            executor.submit(fplugins_load_file, file_path, fPlugins): file_path
            for file_path in file_list
        }
        for future in as_completed(future_to_file):
            _check_stop_requested(projectConfig)
            file_path = future_to_file[future]
            try:
                json_list, save_func = future.result()
                projectConfig.file_save_funcs[file_path] = save_func
                total_chunks.extend(input_splitter.split(json_list, file_path))
                if eng_type == "GenDic":
                    all_jsons.extend(json_list)
            except Exception as exc:
                LOGGER.error(get_text("file_processing_error", GT_LANG, file_path, exc))

    # ---- 2.5 特殊引擎短路：只导出 name 表 / 只生成字典，不进入翻译流程 ----
    if "dump-name" in eng_type:
        _check_stop_requested(projectConfig)
        await dump_name_table_from_chunks(total_chunks, projectConfig)
        return True

    if eng_type == "GenDic":
        _check_stop_requested(projectConfig)
        gptapi = await init_gptapi(projectConfig)
        await gptapi.batch_translate(all_jsons)
        return True

    # ---- 3. 根据 sortBy 决定 chunk 处理顺序 ----
    # name: 按文件名自然序，文件内按 chunk_index 顺序（方便观察进度）
    # size: 按 chunk 大小倒序（让大 chunk 先进入队列，平滑尾部长尾）
    soryBy = projectConfig.getKey("sortBy", "name")
    if soryBy == "name":
        # 按文件分组chunks，保持文件内部的顺序
        file_chunks = {}
        for chunk in total_chunks:
            if chunk.file_path not in file_chunks:
                file_chunks[chunk.file_path] = []
            file_chunks[chunk.file_path].append(chunk)

        # 确保每个文件内的chunks按索引排序
        for file_path in file_chunks:
            file_chunks[file_path].sort(key=lambda x: x.chunk_index)

        # 按照file_list的顺序处理文件，保持文件间的顺序
        ordered_chunks = []
        for file_path in file_list:
            if file_path in file_chunks:
                ordered_chunks.extend(file_chunks[file_path])
    elif soryBy == "size":
        total_chunks.sort(key=lambda x: x.chunk_size, reverse=True)
        ordered_chunks = total_chunks

    total_lines = sum([len(chunk.trans_list) for chunk in ordered_chunks])
    runtime_file_totals, runtime_cache_map = _build_runtime_file_maps(ordered_chunks, input_dir)
    _update_runtime(projectConfig, file_totals=runtime_file_totals, cache_file_display_map=runtime_cache_map)

    # ---- 4. name 替换表（首次运行时自动生成）----
    name_replaceDict_path_xlsx = joinpath(
        projectConfig.getProjectDir(), "name替换表.xlsx"
    )
    name_replaceDict_path_csv = joinpath(
        projectConfig.getProjectDir(), "name替换表.csv"
    )
    name_replaceDict_firstime = False
    if not isPathExists(name_replaceDict_path_csv) and not isPathExists(
        name_replaceDict_path_xlsx
    ):
        await dump_name_table_from_chunks(total_chunks, projectConfig)
        name_replaceDict_firstime = True
    
    # ---- 5. 载入字典（pre/post/gpt）----
    projectConfig.pre_dic = CNormalDic(
        initDictList(pre_dic_list, default_dic_dir, project_dir)
    )
    projectConfig.post_dic = CNormalDic(
        initDictList(post_dic_list, default_dic_dir, project_dir)
    )
    projectConfig.gpt_dic = CGptDict(
        initDictList(gpt_dic_list, default_dic_dir, project_dir)
    )

    if projectConfig.getDictCfgSection().get("sortDict", True):
        projectConfig.pre_dic.sort_dic()
        projectConfig.post_dic.sort_dic()
        projectConfig.gpt_dic.sort_dic()

    # 载入name替换表
    if isPathExists(name_replaceDict_path_csv):
        projectConfig.name_replaceDict = load_name_table(
            name_replaceDict_path_csv, name_replaceDict_firstime,total_chunks,projectConfig
        )
    elif isPathExists(name_replaceDict_path_xlsx):
        projectConfig.name_replaceDict = load_name_table(
            name_replaceDict_path_xlsx, name_replaceDict_firstime,total_chunks,projectConfig
        )

    # ---- 6. 初始化共享的 gptapi 实例（所有 worker 共用同一实例）----
    gptapi = await init_gptapi(projectConfig)

    title_update_task = None  # 初始化任务变量
    auto_tune_task = None
    # 自适应降并发时通过 acquire 占住的槽位数；恢复时再 release
    reserved_permits = 0

    async def set_effective_workers(target: int) -> None:
        """把 effective_workers 调整到 target：
        - 降低：acquire (current-target) 个槽位记为 reserved_permits
        - 提升：release 之前 reserved 的槽位
        通过"预占信号量"而不是直接改 semaphore，避免破坏 asyncio.Semaphore 的内部状态。
        """
        nonlocal reserved_permits

        target = max(1, min(adaptive_state.max_workers, int(target)))
        current = adaptive_state.max_workers - reserved_permits
        if target == current:
            return

        if target < current:
            need_reserve = current - target
            for _ in range(need_reserve):
                _check_stop_requested(projectConfig)
                await semaphore.acquire()
                reserved_permits += 1
        else:
            release_count = min(target - current, reserved_permits)
            for _ in range(release_count):
                semaphore.release()
                reserved_permits -= 1

        adaptive_state.effective_workers = adaptive_state.max_workers - reserved_permits
        projectConfig.runtime_workers_effective = adaptive_state.effective_workers
        projectConfig.runtime_workers_reserved = reserved_permits
        _update_runtime(
            projectConfig,
            workers_configured=adaptive_state.effective_workers,
        )

    # ---- 7. 进入翻译阶段：进度条 + worker 协程池 ----
    with terminal_progress(
        should_print_translation_logs(projectConfig),
        total=total_lines, title="翻译进度", unit=" line", enrich_print=False, dual_line=True,length=30
    ) as bar:
        projectConfig.bar = bar

        # 启动后台任务来更新进度条标题
        title_update_task = asyncio.create_task(
            update_progress_title(bar, semaphore, workersPerProject, projectConfig)
        )

        enable_auto_workers = bool(projectConfig.getKey("autoAdjustWorkers", True))
        if enable_auto_workers and workersPerProject > 1:
            auto_tune_task = asyncio.create_task(
                auto_tune_workers(projectConfig, adaptive_state, set_effective_workers)
            )

        # 用队列 + 哨兵 None 驱动 worker，避免每个 worker 去算自己的分片
        worker_count = max(1, workersPerProject)
        chunk_queue: asyncio.Queue[Optional[SplitChunkMetadata]] = asyncio.Queue()
        for chunk in ordered_chunks:
            _check_stop_requested(projectConfig)
            chunk_queue.put_nowait(chunk)

        # 每个 worker 取到 None 即退出
        for _ in range(worker_count):
            chunk_queue.put_nowait(None)

        async def worker_loop():
            while True:
                _check_stop_requested(projectConfig)
                split_chunk = await chunk_queue.get()
                if split_chunk is None:
                    return
                await doLLMTranslSingleChunk(
                    semaphore,
                    split_chunk=split_chunk,
                    projectConfig=projectConfig,
                    gptapi=gptapi,  # 传递共享的 gptapi 实例
                )

        worker_tasks = [
            asyncio.create_task(worker_loop())
            for _ in range(worker_count)
        ]

        try:
            await asyncio.gather(*worker_tasks)
        except Exception:
            for worker_task in worker_tasks:
                if not worker_task.done():
                    worker_task.cancel()
            await asyncio.gather(*worker_tasks, return_exceptions=True)
            raise
        finally:
            for worker_task in worker_tasks:
                if not worker_task.done():
                    worker_task.cancel()

        try:
            await asyncio.gather(*worker_tasks, return_exceptions=True)
        finally:
            if auto_tune_task:
                auto_tune_task.cancel()
                try:
                    await auto_tune_task
                except asyncio.CancelledError:
                    pass
            if reserved_permits > 0:
                await set_effective_workers(adaptive_state.max_workers)

            # 确保无论 gather 成功还是失败，都取消标题更新任务
            if title_update_task:
                title_update_task.cancel()
                # 等待任务实际被取消（可选，但有助于确保清理）
                try:
                    await title_update_task
                except asyncio.CancelledError:
                    pass  # 捕获预期的取消错误


async def doLLMTranslSingleChunk(
    semaphore: asyncio.Semaphore,
    split_chunk: SplitChunkMetadata,
    projectConfig: CProjectConfig,
    gptapi: Any,  # 添加 gptapi 参数
) -> Tuple[bool, List, List, str, SplitChunkMetadata]:
    """处理单个切片(chunk)的翻译流程。

    顺序：
    1. acquire 信号量 → 进入并发窗口
    2. 前处理（插件 before_src → 字典替换 → after_src）
    3. 读缓存判定命中/未命中（含 append 日志合并）
    4. 未命中部分调 gptapi.batch_translate；若启用则做校对
    5. 后处理（恢复符号、post 字典、插件 after_dst）
    6. 如果该文件所有 chunk 都完成，触发 postprocess_results 合并写出+快照缓存
    """

    async with semaphore:
        _check_stop_requested(projectConfig)
        st = time()
        proj_dir = projectConfig.getProjectDir()
        input_dir = projectConfig.getInputPath()
        output_dir = projectConfig.getOutputPath()
        cache_dir = projectConfig.getCachePath()
        pre_dic = projectConfig.pre_dic
        post_dic = projectConfig.post_dic
        gpt_dic = projectConfig.gpt_dic
        file_path = split_chunk.file_path
        file_name = (
            file_path.replace(input_dir, "").lstrip(os_sep).replace(os_sep, "-}")
        )  # 多级文件夹
        tPlugins = projectConfig.tPlugins
        eng_type = projectConfig.select_translator

        total_splits = split_chunk.total_chunks
        file_index = split_chunk.chunk_index
        input_file_path = file_path
        output_file_path = input_file_path.replace(input_dir, output_dir)

        cache_file_path = joinpath(
            cache_dir,
            file_name + (f"_{file_index}" if total_splits > 1 else ""),
        )

        part_info = f" (part {file_index+1}/{total_splits})" if total_splits > 1 else ""
        _update_runtime(
            projectConfig,
            current_file=file_name,
            workers_configured=int(
                getattr(
                    projectConfig,
                    "runtime_workers_effective",
                    projectConfig.getKey("workersPerProject") or 1,
                )
            ),
        )
        LOGGER.info(f">>> 开始翻译 (project_dir){split_chunk.file_path.replace(proj_dir,'')}")
        LOGGER.debug(f"文件 {file_name} 分块 {file_index+1}/{total_splits}:")
        LOGGER.debug(f"  开始索引: {split_chunk.start_index}")
        LOGGER.debug(f"  结束索引: {split_chunk.end_index}")
        LOGGER.debug(f"  非交叉大小: {split_chunk.chunk_non_cross_size}")
        LOGGER.debug(f"  实际大小: {split_chunk.chunk_size}")
        LOGGER.debug(f"  交叉数量: {split_chunk.cross_num}")

        # 翻译前处理
        preprocess_trans_list(split_chunk.trans_list, projectConfig, pre_dic, tPlugins)

        translist_hit, translist_unhit = await get_transCache_from_json(
            split_chunk.trans_list,
            cache_file_path,
            retry_failed=projectConfig.getKey("retranslFail"),
            proofread=False,
            retran_key=projectConfig.getKey("retranslKey"),
            eng_type=eng_type,
        )

        if len(translist_hit) > 0:
            projectConfig.bar(len(translist_hit), skipped=True) # 更新进度条

        if len(translist_unhit) > 0:
            _check_stop_requested(projectConfig)
            # 执行翻译
            await gptapi.batch_translate(
                file_name + (f"_{file_index}" if total_splits > 1 else ""),
                cache_file_path,
                split_chunk.trans_list,
                projectConfig.getKey("gpt.numPerRequestTranslate"),
                retry_failed=projectConfig.getKey("retranslFail"),
                gpt_dic=gpt_dic,
                retran_key=projectConfig.getKey("retranslKey"),
                translist_hit=translist_hit,
                translist_unhit=translist_unhit,
            )

            # 执行校对（如果启用）
            if projectConfig.getKey("gpt.enableProofRead"):
                _check_stop_requested(projectConfig)
                if "gpt4" in eng_type:
                    await gptapi.batch_translate(
                        file_name,
                        cache_file_path,
                        split_chunk.trans_list,
                        projectConfig.getKey("gpt.numPerRequestProofRead"),
                        retry_failed=projectConfig.getKey("retranslFail"),
                        gpt_dic=gpt_dic,
                        proofread=True,
                        retran_key=projectConfig.getKey("retranslKey"),
                    )
                else:
                    LOGGER.warning("当前引擎不支持校对，跳过校对步骤")
            gptapi.clean_up()

        # 翻译后处理
        _check_stop_requested(projectConfig)
        postprocess_trans_list(split_chunk.trans_list, projectConfig, post_dic, tPlugins)

        et = time()
        LOGGER.info(
            get_text(
                "file_translation_completed", GT_LANG, file_name, part_info, et - st
            )
        )

        # 登记本 chunk 已完成；只有当"同一文件的全部 chunk"都完成时才做整文件后处理
        split_chunk.update_file_finished_chunk()
        if split_chunk.is_file_finished():
            LOGGER.debug(get_text("file_chunks_completed", GT_LANG, file_name))
            await postprocess_results(
                split_chunk.get_file_finished_chunks(), projectConfig
            )

        _update_runtime(projectConfig, current_file=file_name)


async def postprocess_results(
    resultChunks: List[SplitChunkMetadata],
    projectConfig: CProjectConfig,
):
    """单个文件翻译完成后的收尾工作。

    对每个 chunk 逐一：find_problems 标注问题 → save_transCache_to_json(post_save=True)
    写完整 jsonl 快照（这也是唯一一次把 append 日志合并入主快照的时机）。
    随后合并所有 chunk 的结果，套用 name 替换表并经文件插件写出最终译文。
    """

    proj_dir = projectConfig.getProjectDir()
    input_dir = projectConfig.getInputPath()
    output_dir = projectConfig.getOutputPath()
    cache_dir = projectConfig.getCachePath()
    eng_type = projectConfig.select_translator
    gpt_dic = projectConfig.gpt_dic
    name_replaceDict = projectConfig.name_replaceDict

    # 对每个分块执行错误检查和缓存保存
    for i, chunk in enumerate(resultChunks):
        trans_list = chunk.trans_list
        file_path = chunk.file_path
        cache_file_path = joinpath(
            cache_dir,
            file_path.replace(input_dir, "").lstrip(os_sep).replace(os_sep, "-}")
            + (f"_{chunk.chunk_index}" if chunk.total_chunks > 1 else ""),
        )

        # rebuildr 是"只重建输出文件"模式，不应修改缓存；其余引擎正常刷新
        if eng_type != "rebuildr":
            find_problems(trans_list, projectConfig, gpt_dic)
            # post_save=True → 写完整快照并删除对应 .append 日志（即合并 jsonl）
            await save_transCache_to_json(trans_list, cache_file_path, post_save=True)

    # 使用output_combiner合并结果，即使只有一个结果
    all_trans_list, all_json_list = DictionaryCombiner.combine(resultChunks)
    LOGGER.debug(f"合并后总行数: {len(all_trans_list)}")
    file_path = resultChunks[0].file_path
    output_file_path = file_path.replace(input_dir, output_dir)
    save_func = projectConfig.file_save_funcs.get(file_path, save_json)

    if all_trans_list and all_json_list:
        final_result = update_json_with_transList(
            all_trans_list, all_json_list, name_replaceDict
        )
        makedirs(dirname(output_file_path), exist_ok=True)
        save_func(output_file_path, final_result)
        LOGGER.info(f"+++ 结果保存 (project_dir){output_file_path.replace(proj_dir,'')}")  # 添加保存确认日志


async def init_gptapi(
    projectConfig: CProjectConfig,
):
    """
    根据引擎类型获取相应的API实例（延迟导入后端模块以避免不必要依赖）。

    参数:
    projectConfig: 项目配置对象
    eng_type: 引擎类型
    endpoint: API端点（如果适用）
    proxyPool: 代理池（如果适用）
    tokenPool: Token池

    返回:
    相应的API实例
    """
    proxyPool = projectConfig.proxyPool
    tokenPool = projectConfig.tokenPool
    sakuraEndpointQueue = projectConfig.endpointQueue
    eng_type = projectConfig.select_translator

    match eng_type:
        case "ForGal-tsv":
            from GalTransl.Backend.ForGalTsvTranslate import ForGalTsvTranslate
            return ForGalTsvTranslate(projectConfig, eng_type, proxyPool, tokenPool)
        case "ForNovel":
            from GalTransl.Backend.ForNovelTranslate import ForNovelTranslate
            return ForNovelTranslate(projectConfig, eng_type, proxyPool, tokenPool)
        case "ForGal-json" | "r1":
            from GalTransl.Backend.ForGalJsonTranslate import ForGalJsonTranslate
            return ForGalJsonTranslate(projectConfig, eng_type, proxyPool, tokenPool)
        case "sakura-v1.0" | "galtransl-v3":
            from GalTransl.Backend.SakuraTranslate import CSakuraTranslate
            sakura_endpoint = await sakuraEndpointQueue.get()
            if sakuraEndpointQueue is None:
                raise ValueError(f"Endpoint is required for engine type {eng_type}")
            return CSakuraTranslate(projectConfig, eng_type, sakura_endpoint, proxyPool)
        case "rebuildr" | "rebuilda" | "dump-name":
            from GalTransl.Backend.RebuildTranslate import CRebuildTranslate
            return CRebuildTranslate(projectConfig, eng_type)
        case "GenDic":
            from GalTransl.Backend.GenDic import GenDic
            return GenDic(projectConfig, eng_type, proxyPool, tokenPool)
        case _:
            raise ValueError(f"不支持的翻译引擎类型 {eng_type}")


def fplugins_load_file(file_path: str, fPlugins: list) -> Tuple[List[Dict], Any]:
    """按顺序尝试每个文件插件解析 file_path。

    第一个成功的插件决定解析结果与对应的保存函数 save_func。
    返回 (json_list, save_func)；若所有插件都失败则断言报错。
    """
    result = None
    save_func = None
    for plugin in fPlugins:

        if isinstance(plugin, str):
            LOGGER.warning(f"跳过无效的插件项: {plugin}")
            continue
        try:
            result = plugin.plugin_object.load_file(file_path)
            save_func = plugin.plugin_object.save_file
            break
        except TypeError as e:
            LOGGER.error(
                f"{file_path} 不是文件插件'{getattr(plugin, 'name', 'Unknown')}'支持的格式：{e}"
            )
        except Exception as e:
            LOGGER.error(
                f"插件 {getattr(plugin, 'name', 'Unknown')} 读取文件 {file_path} 出错: {e}"
            )

    assert result is not None, get_text("file_load_failed", GT_LANG, file_path)

    assert isinstance(result, list), f"文件 {file_path} 不是列表"

    return result, save_func
