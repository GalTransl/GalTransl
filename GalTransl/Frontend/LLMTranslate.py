from typing import List, Dict, Any, Optional, Union, Tuple
from os import makedirs, cpu_count, sep as os_sep
from os.path import join as joinpath, exists as isPathExists, dirname
from alive_progress import alive_bar
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
    EqualPartsSplitter,
)


async def update_progress_title(
    bar, semaphore, workersPerProject: int, projectConfig: CProjectConfig
):
    """异步任务，用于动态更新 alive_bar 的标题以显示活动工作线程数。"""
    base_title = "翻译进度"
    while True:
        try:
            # 计算当前活动的任务数
            # semaphore.acquire() 会减少 _value，semaphore.release() 会增加 _value
            # 因此，活动任务数 = 总容量 - 当前可用容量
            active_workers = workersPerProject - semaphore._value
            # 确保 active_workers 不会是负数（以防万一）
            active_workers = max(0, active_workers)
            if active_workers == 0:
                projectConfig.active_workers = workersPerProject
            else:
                projectConfig.active_workers = active_workers
            # 更新标题
            new_title = f"{base_title} [活跃任务: {active_workers}/{workersPerProject}]"
            bar.title(new_title)

            # 每隔一段时间更新一次，避免过于频繁
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            # 当任务被取消时，设置最终标题并退出循环
            bar.title(f"{base_title} [处理完成]")
            break
        except Exception as e:
            # 记录任何其他异常并停止更新
            LOGGER.error(f"更新进度条标题时出错: {e}")
            bar.title(f"{base_title} [更新出错]")
            break


async def doLLMTranslate(
    projectConfig: CProjectConfig,
) -> bool:

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
    fPlugins = projectConfig.fPlugins
    tPlugins = projectConfig.tPlugins
    eng_type = projectConfig.select_translator
    input_splitter = projectConfig.input_splitter
    SplitChunkMetadata.clear_file_finished_chunk()
    total_chunks = []

    makedirs(output_dir, exist_ok=True)
    makedirs(cache_dir, exist_ok=True)

    # 语言设置
    if val := projectConfig.getKey("language"):
        sp = val.split("2")
        projectConfig.source_lang = sp[0]
        projectConfig.target_lang = sp[1]

    # 获取待翻译文件列表
    file_list = get_file_list(projectConfig.getInputPath())
    if not file_list:
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
    # 读取所有文件获得total_chunks列表
    with ThreadPoolExecutor(max_workers=cpu_count()) as executor:
        future_to_file = {
            executor.submit(fplugins_load_file, file_path, fPlugins): file_path
            for file_path in file_list
        }
        for future in as_completed(future_to_file):
            file_path = future_to_file[future]
            try:
                json_list, save_func = future.result()
                projectConfig.file_save_funcs[file_path] = save_func
                total_chunks.extend(input_splitter.split(json_list, file_path))
                if eng_type == "GenDic":
                    all_jsons.extend(json_list)
            except Exception as exc:
                LOGGER.error(get_text("file_processing_error", GT_LANG, file_path, exc))

    if "dump-name" in eng_type:
        await dump_name_table_from_chunks(total_chunks, projectConfig)
        return True

    if eng_type == "GenDic":
        gptapi = await init_gptapi(projectConfig)
        await gptapi.batch_translate(all_jsons)
        return True

    async def run_task(task_func):
        try:
            result = await task_func
            return result
        except Exception as e:
            LOGGER.error(get_text("task_execution_failed", GT_LANG, e))
            return None

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

    # 初始生成name替换表
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
    
    # 载入字典
    projectConfig.pre_dic = CNormalDic(
        initDictList(pre_dic_list, default_dic_dir, project_dir)
    )
    projectConfig.post_dic = CNormalDic(
        initDictList(post_dic_list, default_dic_dir, project_dir)
    )
    projectConfig.gpt_dic = CGptDict(
        initDictList(gpt_dic_list, default_dic_dir, project_dir)
    )
    if projectConfig.getDictCfgSection().get("sortPrePostDict", True):
        projectConfig.pre_dic.sort_dic()
        projectConfig.post_dic.sort_dic()
    elif projectConfig.getDictCfgSection().get("sortDict", True):
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

    # 初始化共享的 gptapi 实例
    gptapi = await init_gptapi(projectConfig)

    title_update_task = None  # 初始化任务变量
    projectConfig.active_workers = 1
    with alive_bar(
        total=total_lines, title="翻译进度", unit=" line", enrich_print=False, dual_line=True
    ) as bar:
        projectConfig.bar = bar

        # 启动后台任务来更新进度条标题
        title_update_task = asyncio.create_task(
            update_progress_title(bar, semaphore, workersPerProject, projectConfig)
        )

        # 创建所有翻译任务
        all_tasks = []
        for chunk in ordered_chunks:
            all_tasks.append(
                doLLMTranslSingleChunk(
                    semaphore,
                    split_chunk=chunk,
                    projectConfig=projectConfig,
                    gptapi=gptapi,  # 传递共享的 gptapi 实例
                )
            )

        try:
            # 使用信号量控制并发数量，同时启动所有翻译任务
            await asyncio.gather(*[run_task(task) for task in all_tasks])
        finally:
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

    async with semaphore:
        print(f"线程 {semaphore._value} 开始翻译 {split_chunk.file_path}")
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
        # LOGGER.info(f"开始翻译 {file_name}{part_info}, 引擎类型: {eng_type}")

        LOGGER.debug(f"文件 {file_name} 分块 {file_index+1}/{total_splits}:")
        LOGGER.debug(f"  开始索引: {split_chunk.start_index}")
        LOGGER.debug(f"  结束索引: {split_chunk.end_index}")
        LOGGER.debug(f"  非交叉大小: {split_chunk.chunk_non_cross_size}")
        LOGGER.debug(f"  实际大小: {split_chunk.chunk_size}")
        LOGGER.debug(f"  交叉数量: {split_chunk.cross_num}")

        # 翻译前处理
        for tran in split_chunk.trans_list:
            for plugin in tPlugins:
                try:
                    tran = plugin.plugin_object.before_src_processed(tran)
                except Exception as e:
                    LOGGER.error(
                        get_text("plugin_execution_failed", GT_LANG, plugin.name, e)
                    )

            if projectConfig.getFilePlugin() in [
                "file_galtransl_json",
                "file_mtbench_chrf",
            ]:
                tran.analyse_dialogue()

            tran.post_jp = pre_dic.do_replace(tran.post_jp, tran)

            if projectConfig.getDictCfgSection("usePreDictInName"):
                if isinstance(tran.speaker, str) and isinstance(tran._speaker, str):
                    tran.speaker = pre_dic.do_replace(tran.speaker, tran)
            for plugin in tPlugins:
                try:
                    tran = plugin.plugin_object.after_src_processed(tran)
                except Exception as e:
                    LOGGER.error(
                        get_text("plugin_execution_failed", GT_LANG, plugin.name, e)
                    )

        translist_hit, translist_unhit = get_transCache_from_json(
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
            # 执行翻译
            await gptapi.batch_translate(
                file_name,
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
        for tran in split_chunk.trans_list:
            for plugin in tPlugins:
                try:
                    tran = plugin.plugin_object.before_dst_processed(tran)
                except Exception as e:
                    LOGGER.error(f" 插件 {plugin.name} 执行失败: {e}")

            tran.recover_dialogue_symbol()
            tran.post_zh = post_dic.do_replace(tran.post_zh, tran)

            for plugin in tPlugins:
                try:
                    tran = plugin.plugin_object.after_dst_processed(tran)
                except Exception as e:
                    LOGGER.error(
                        get_text("plugin_execution_failed", GT_LANG, plugin.name, e)
                    )

        et = time()
        LOGGER.info(
            get_text(
                "file_translation_completed", GT_LANG, file_name, part_info, et - st
            )
        )

        split_chunk.update_file_finished_chunk()
        # 检查是否该文件的所有chunk都翻译完成
        if split_chunk.is_file_finished():
            LOGGER.debug(get_text("file_chunks_completed", GT_LANG, file_name))
            await postprocess_results(
                split_chunk.get_file_finished_chunks(), projectConfig
            )


async def postprocess_results(
    resultChunks: List[SplitChunkMetadata],
    projectConfig: CProjectConfig,
):

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

        if eng_type != "rebuildr":
            find_problems(trans_list, projectConfig, gpt_dic)
            save_transCache_to_json(trans_list, cache_file_path, post_save=True)

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
        LOGGER.info(f"已保存文件: {output_file_path}")  # 添加保存确认日志


async def init_gptapi(
    projectConfig: CProjectConfig,
):
    """
    根据引擎类型获取相应的API实例。

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
        case "ForGal":
            from GalTransl.Backend.ForGalTranslate import ForGalTranslate

            return ForGalTranslate(projectConfig, eng_type, proxyPool, tokenPool)
        case "gpt4" | "gpt4-turbo" | "r1":
            from GalTransl.Backend.GPT4Translate import CGPT4Translate

            return CGPT4Translate(projectConfig, eng_type, proxyPool, tokenPool)
        case "sakura-009" | "sakura-v1.0" | "galtransl-v2.5" | "galtransl-v3":
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
