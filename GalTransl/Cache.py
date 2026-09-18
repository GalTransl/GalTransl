"""
缓存机制
"""

from GalTransl.CSentense import CSentense, CTransList
from GalTransl.ProblemFilter import filter_problem_text, normalize_problem_filter_keys
from GalTransl import LOGGER
from typing import List
import orjson
import os
import asyncio
from GalTransl.i18n import get_text,GT_LANG
import aiofiles

# 整文件重写的中间文件后缀：先写 <缓存>.json.tmp，再替换到 <缓存>.json
# （见 save_transCache_to_json / _compact_cache_from_append）。正常情况下它活不过一次替换，
# 缓存目录里见到它，就是上次中断或被占用留下的遗物——启动时扫掉（见 cleanup_stale_cache_temp_files）。
CACHE_TEMP_SUFFIX = ".json.tmp"

# 缓存JSON key映射：新key -> 旧key（用于兼容读取旧缓存）
_CACHE_KEY_COMPAT = {
    "pre_src": "pre_jp",
    "post_src": "post_jp",
    "pre_dst": "pre_zh",
    "proofread_dst": "proofread_zh",
    "post_dst_preview": "post_zh_preview",
    # 旧名「存疑内容」→ 新名「校对批注」：只读兼容，写回一律用新键
    "proofread_comment": "doub_content",
}

def _cache_get(cache_obj: dict, key: str, default=None):
    """从缓存对象中读取值，优先使用新key名，回退到旧key名（兼容旧缓存）"""
    if key in cache_obj:
        return cache_obj[key]
    old_key = _CACHE_KEY_COMPAT.get(key)
    if old_key and old_key in cache_obj:
        return cache_obj[old_key]
    return default

def _cache_has(cache_obj: dict, key: str) -> bool:
    """检查缓存对象中是否包含某个key（兼容新旧key名）"""
    if key in cache_obj:
        return True
    old_key = _CACHE_KEY_COMPAT.get(key)
    if old_key and old_key in cache_obj:
        return True
    return False


# 未命中缓存的原因码：查缓存时写在 tran.cache_miss_reason 上。
# 重建（rebuilda/rebuildr）不翻译、只能按现有缓存重刷译文与结果，一旦有未命中就整个失败
# （见 Backend/RebuildTranslate）；带上原因码，那里才能报出"哪几句、为什么"，而不是一句
# 笼统的「缓存不完整」——最容易被误读成"这个文件还没翻"。
MISS_KEY_NOT_FOUND = "key_not_found"
MISS_POST_SRC_CHANGED = "post_src_changed"
MISS_PRE_DST_EMPTY = "pre_dst_empty"
MISS_TRANSLATE_FAILED = "translate_failed"
MISS_RETRAN_KEY = "retran_key"
MISS_RETRAN_PROBLEM = "retran_problem"
MISS_PROOFREAD_MISSING = "proofread_missing"


def _mark_cache_miss(tran: CSentense, reason: str) -> None:
    """记下这句没命中缓存的原因（只记第一个：后面的检查都是在前一个没过之后才跑的）。"""
    if not tran.cache_miss_reason:
        tran.cache_miss_reason = reason


def _replace_cache_file(temp_file_path: str, cache_file_path: str) -> None:
    """把写好的临时文件换到正式位置（原子替换）。

    用 os.replace 而不是 shutil.move：Windows 上 os.rename 覆盖已存在文件必然抛
    FileExistsError，shutil.move 于是退化成 copy2 + unlink——那是**原地截断重写**，
    写到一半崩了就把正式缓存毁成半截 JSON，没写完时临时文件还留在原地（缓存目录里那些
    .json.tmp 就是这么来的）。os.replace 两边都是原子替换：读到的要么是旧的完整文件、
    要么是新的完整文件；失败时正式文件一字不动。
    """
    os.replace(temp_file_path, cache_file_path)


def cleanup_stale_cache_temp_files(cache_dir: str) -> int:
    """删掉缓存目录里残留的 <缓存>.json.tmp，返回删掉几个。

    只在任务启动时调用：那一刻本项目没有写入者（server 保证一个项目同时只有一个任务，
    桌面端改缓存是直接写文件、不走临时文件），所以这里看到的 .json.tmp 一定是上次中断或
    被占用留下的，删掉安全。只认 .json.tmp——同一个目录里别的东西（缓存本身、.append.jsonl）
    一概不碰。
    """
    if not cache_dir or not os.path.isdir(cache_dir):
        return 0
    removed = 0
    for name in os.listdir(cache_dir):
        if not name.endswith(CACHE_TEMP_SUFFIX):
            continue
        path = os.path.join(cache_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError as exc:  # 被占用等：留着，下次再说
            LOGGER.warning(f"[cache]清理残留临时文件失败：{path}: {exc}")
    return removed


_CACHE_APPEND_SUFFIX = ".append.jsonl"
_CACHE_APPEND_WRITE_RETRY_TIMES = 6
_CACHE_APPEND_WRITE_RETRY_DELAY = 0.08


def _append_cache_file_path(cache_file_path: str) -> str:
    return cache_file_path + _CACHE_APPEND_SUFFIX


def _is_windows_file_lock_error(err: BaseException) -> bool:
    winerror = getattr(err, "winerror", None)
    if winerror in (32, 33):
        return True
    return isinstance(err, PermissionError)


def _record_runtime_cache_error(project_dir: str, *, message: str, filename: str = "", level: str = "warning") -> None:
    if not project_dir:
        return
    try:
        from GalTransl.server import record_runtime_error

        record_runtime_error(
            project_dir,
            kind="cache",
            message=message,
            filename=filename,
            level=level,
        )
    except Exception:
        return


async def _write_append_entries_with_retry(append_file_path: str, append_entries: list[dict]) -> None:
    if not append_entries:
        return

    last_error = None
    for attempt in range(_CACHE_APPEND_WRITE_RETRY_TIMES):
        try:
            async with aiofiles.open(append_file_path, mode="ab") as f:
                for entry in append_entries:
                    await f.write(orjson.dumps(entry))
                    await f.write(b"\n")
            return
        except Exception as e:
            last_error = e
            if not _is_windows_file_lock_error(e):
                raise
            if attempt >= _CACHE_APPEND_WRITE_RETRY_TIMES - 1:
                break
            await asyncio.sleep(_CACHE_APPEND_WRITE_RETRY_DELAY * (attempt + 1))

    raise last_error


def _build_cache_key_for_tran(tran) -> str:
    line_now, line_priv, line_next = "", "None", "None"
    line_now = f"{tran.speaker}{tran.pre_src}"

    prev_tran = tran.prev_tran
    while prev_tran and prev_tran.post_src == "":
        prev_tran = prev_tran.prev_tran
    if prev_tran:
        line_priv = f"{prev_tran.speaker}{prev_tran.pre_src}"

    next_tran = tran.next_tran
    while next_tran and next_tran.post_src == "":
        next_tran = next_tran.next_tran
    if next_tran:
        line_next = f"{next_tran.speaker}{next_tran.pre_src}"

    line_priv = "None" if line_priv == "" else line_priv
    line_next = "None" if line_next == "" else line_next
    return line_priv + line_now + line_next


def _build_cache_obj(tran, post_save: bool = False):
    if tran.post_src == "":
        return None
    if tran.pre_dst == "":
        return None

    cache_obj = {
        "index": tran.index,
        "name": tran.speaker,
        "pre_src": tran.pre_src,
        "post_src": tran.post_src,
        "pre_dst": tran.pre_dst,
    }
    cache_obj["proofread_dst"] = tran.proofread_zh

    if post_save and tran.problem != "":
        cache_obj["problem"] = tran.problem

    if getattr(tran, "skip_check", False):
        cache_obj["skip_check"] = True

    cache_obj["trans_by"] = tran.trans_by
    cache_obj["proofread_by"] = tran.proofread_by

    if tran.trans_conf != 0:
        cache_obj["trans_conf"] = tran.trans_conf
    if tran.proofread_comment != "":
        cache_obj["proofread_comment"] = tran.proofread_comment
    if tran.unknown_proper_noun != "":
        cache_obj["unknown_proper_noun"] = tran.unknown_proper_noun
    if post_save:
        cache_obj["post_dst_preview"] = tran.post_dst

    return cache_obj


def _build_cache_dict_from_snapshot(cache_list: list) -> tuple[dict, list[str]]:
    cache_dict = {}
    cache_order: list[str] = []
    for i, cache in enumerate(cache_list):
        line_now, line_priv, line_next = "", "None", "None"
        line_now = f'{cache.get("name", "")}{_cache_get(cache, "pre_src", "")}'
        if i > 0:
            line_priv = f'{cache_list[i-1].get("name", "")}{_cache_get(cache_list[i-1], "pre_src", "")}'
        if i < len(cache_list) - 1:
            line_next = f'{cache_list[i+1].get("name", "")}{_cache_get(cache_list[i+1], "pre_src", "")}'
        line_priv = "None" if line_priv == "" else line_priv
        line_next = "None" if line_next == "" else line_next
        cache_key = line_priv + line_now + line_next
        if cache_key not in cache_dict:
            cache_order.append(cache_key)
        cache_dict[cache_key] = cache
    return cache_dict, cache_order


async def _compact_cache_from_append(cache_file_path: str, append_file_path: str) -> None:
    cache_list = []
    if os.path.exists(cache_file_path):
        async with aiofiles.open(cache_file_path, mode="rb") as f:
            raw = await f.read()
            if raw:
                cache_list = orjson.loads(raw)

    cache_dict, cache_order = _build_cache_dict_from_snapshot(cache_list)

    if os.path.exists(append_file_path):
        async with aiofiles.open(append_file_path, mode="rb") as f:
            append_raw = await f.read()
        for line in append_raw.splitlines():
            if not line:
                continue
            try:
                cache_obj = orjson.loads(line)
            except Exception:
                continue
            cache_key = str(cache_obj.pop("__cache_key", ""))
            if not cache_key:
                continue
            if cache_key not in cache_dict:
                cache_order.append(cache_key)
                cache_dict[cache_key] = cache_obj
            else:
                # 以快照为基合并：append 提供的键覆盖快照，
                # append 未提供的键（如 problem 等派生字段）保留。
                # 这样中途被打断后再启动 compaction 不会把 problem 字段抹掉，
                # 避免 retranslKey-by-problem 失效。
                merged_obj = dict(cache_dict[cache_key])
                merged_obj.update(cache_obj)
                cache_dict[cache_key] = merged_obj

    merged_cache = [cache_dict[key] for key in cache_order if key in cache_dict]

    temp_file_path = cache_file_path + ".tmp"
    async with aiofiles.open(temp_file_path, mode="wb") as f:
        await f.write(orjson.dumps(merged_cache, option=orjson.OPT_INDENT_2))
    _replace_cache_file(temp_file_path, cache_file_path)

    if os.path.exists(append_file_path):
        os.remove(append_file_path)


async def compact_cache_append_logs(cache_dir: str) -> int:
    if not cache_dir or not os.path.isdir(cache_dir):
        return 0

    compacted_count = 0
    for name in os.listdir(cache_dir):
        if not name.endswith(_CACHE_APPEND_SUFFIX):
            continue

        append_file_path = os.path.join(cache_dir, name)
        cache_file_path = append_file_path[: -len(_CACHE_APPEND_SUFFIX)]
        try:
            await _compact_cache_from_append(cache_file_path, append_file_path)
            compacted_count += 1
        except Exception as e:
            LOGGER.warning(f"[cache]压缩append缓存失败：{append_file_path}: {e}")

    return compacted_count


async def save_transCache_to_json(trans_list: CTransList, cache_file_path, post_save=False, project_dir: str = ""):
    """
    此函数将翻译缓存保存到 JSON 文件中。
    使用原子写入机制，避免程序异常关闭时写入不完整的问题。

    Args:
        trans_list (CTransList): 要保存的翻译列表。
        cache_file_path (str): 要保存到的 JSON 文件的路径。
        post_save (bool, optional): 是否是翻译结束后的存储。默认为 False。
        project_dir (str, optional): 运行时项目目录，用于上报工作台最近错误卡片。默认为空。
    """
    if not cache_file_path.endswith(".json"):
        cache_file_path += ".json"

    append_file_path = _append_cache_file_path(cache_file_path)

    # 创建临时文件路径，用于原子写入
    temp_file_path = cache_file_path + ".tmp"

    cache_json = []
    append_entries = []

    for tran in trans_list:
        cache_obj = _build_cache_obj(tran, post_save=post_save)
        if cache_obj is None:
            continue
        cache_json.append(cache_obj)
        cache_key = _build_cache_key_for_tran(tran)
        if cache_key:
            append_obj = dict(cache_obj)
            append_obj["__cache_key"] = cache_key
            append_entries.append(append_obj)

    try:
        if post_save:
            # 翻译完成后做一次完整快照，并清理append日志
            async with aiofiles.open(temp_file_path, mode="wb") as f:
                json_data = orjson.dumps(cache_json, option=orjson.OPT_INDENT_2)
                await f.write(json_data)
            _replace_cache_file(temp_file_path, cache_file_path)
            if os.path.exists(append_file_path):
                try:
                    os.remove(append_file_path)
                except Exception as e:
                    if _is_windows_file_lock_error(e):
                        warn_msg = f"[cache]清理append缓存文件失败(被占用，稍后自动恢复)：{append_file_path}"
                        LOGGER.warning(warn_msg)
                        _record_runtime_cache_error(
                            project_dir,
                            message=warn_msg,
                            filename=os.path.basename(append_file_path),
                        )
                    else:
                        raise
        else:
            # 增量写入append日志，避免频繁整文件重写
            if append_entries:
                try:
                    await _write_append_entries_with_retry(append_file_path, append_entries)
                except Exception as e:
                    if _is_windows_file_lock_error(e):
                        warn_msg = f"[cache]增量缓存写入失败(文件被占用，已跳过本次写入)：{append_file_path}"
                        LOGGER.warning(warn_msg)
                        _record_runtime_cache_error(
                            project_dir,
                            message=warn_msg,
                            filename=os.path.basename(append_file_path),
                        )
                        return
                    raise
    except Exception as e:
        LOGGER.error(f"[cache]保存缓存失败：{str(e)}")
        
        # 清理临时文件
        if os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except:
                pass
        
        # 重新抛出异常
        raise e


async def get_transCache_from_json(
    trans_list: CTransList,
    cache_file_path,
    retry_failed=False,
    proofread=False,
    retran_key="",
    load_post_src=False,
    ignr_post_src=False,
    eng_type="",
    problem_filter_keys=None,
):
    """
    此函数从 JSON 文件中检索翻译缓存，并相应地更新翻译列表。

    Args:
        trans_list (CTransList): 要检索的翻译列表。
        cache_file_path (str): 包含翻译缓存的 JSON 文件的路径。
        retry_failed (bool, optional): 是否重试失败的翻译。默认为 False。
        proofread (bool, optional): 是否是校对模式。默认为 False。
        retran_key (str or list, optional): 重译关键字，可以是字符串或字符串列表。默认为空字符串。
        load_post_src (bool, optional): 不检查post_src是否被改变, 且直接使用cache的post_src。默认为 False。
        ignr_post_src (bool, optional): 仅不检查post_src是否被改变。默认为 False。

    Returns:
        Tuple[List[CTrans], List[CTrans]]: 包含两个列表的元组：击中缓存的翻译列表和未击中缓存的翻译列表。
    """
    if not cache_file_path.endswith(".json"):
        if not os.path.exists(cache_file_path):
            cache_file_path += ".json"

    translist_hit = []
    problem_filter_keys = normalize_problem_filter_keys(problem_filter_keys)
    translist_unhit = []
    cache_dict = {}
    if os.path.exists(cache_file_path):
        async with aiofiles.open(cache_file_path, encoding="utf8") as f:
            try:
                cache_dictList = orjson.loads(await f.read())
                for i, cache in enumerate(cache_dictList):
                    line_now, line_priv, line_next = "", "None", "None"
                    line_now = f'{cache["name"]}{_cache_get(cache, "pre_src")}'
                    if i > 0:
                        line_priv = f'{cache_dictList[i-1]["name"]}{_cache_get(cache_dictList[i-1], "pre_src")}'
                    if i < len(cache_dictList) - 1:
                        line_next = f'{cache_dictList[i+1]["name"]}{_cache_get(cache_dictList[i+1], "pre_src")}'
                    line_priv = "None" if line_priv == "" else line_priv
                    line_next = "None" if line_next == "" else line_next
                    cache_dict[line_priv + line_now + line_next] = cache
            except Exception as e:
                LOGGER.error(str(e))
                LOGGER.error(get_text("cache_read_error", GT_LANG, cache_file_path))
                custom_msg = get_text("cache_read_error", GT_LANG, cache_file_path) + f": {str(e)}"
                raise RuntimeError(custom_msg) from e

    append_file_path = _append_cache_file_path(cache_file_path)
    if os.path.exists(append_file_path):
        try:
            async with aiofiles.open(append_file_path, mode="rb") as f:
                append_raw = await f.read()
            for line in append_raw.splitlines():
                if not line:
                    continue
                try:
                    cache_obj = orjson.loads(line)
                except Exception:
                    continue
                cache_key = str(cache_obj.pop("__cache_key", ""))
                if not cache_key:
                    continue
                if cache_key in cache_dict:
                    # 与 _compact_cache_from_append 保持一致的合并策略：
                    # 保留快照中 append 未提供的派生字段（如 problem），
                    # 这样 retranslKey-by-problem 检测仍能基于最近一次
                    # 完整 post_save 记录的 problem 正确触发。
                    merged_obj = dict(cache_dict[cache_key])
                    merged_obj.update(cache_obj)
                    cache_dict[cache_key] = merged_obj
                else:
                    cache_dict[cache_key] = cache_obj
        except Exception as e:
            LOGGER.warning(f"[cache]读取append缓存失败：{append_file_path}: {e}")


    for tran in trans_list:
        tran.cache_miss_reason = ""  # 每句只记本次查询的结果，别留着上一轮的
        # 忽略jp为空的句子
        if tran.pre_src == "" or tran.post_src == "":
            tran.pre_dst, tran.post_dst = "", ""
            translist_hit.append(tran)
            continue
        # 忽略在读取缓存前pre_dst就有值的句子
        if tran.pre_dst != "":
            tran.post_dst = tran.pre_dst
            translist_hit.append(tran)
            continue

        line_now, line_priv, line_next = "", "None", "None"
        line_now = f"{tran.speaker}{tran.pre_src}"
        prev_tran = tran.prev_tran
        # 找非空前句
        while prev_tran and prev_tran.post_src == "":
            prev_tran = prev_tran.prev_tran
        if prev_tran:
            line_priv = f"{prev_tran.speaker}{prev_tran.pre_src}"
        # 找非空后句
        next_tran = tran.next_tran
        while next_tran and next_tran.post_src == "":
            next_tran = next_tran.next_tran
        if next_tran:
            line_next = f"{next_tran.speaker}{next_tran.pre_src}"

        line_priv = "None" if line_priv == "" else line_priv
        line_next = "None" if line_next == "" else line_next
        cache_key = line_priv + line_now + line_next

        # cache_key不在缓存
        if cache_key not in cache_dict:
            _mark_cache_miss(tran, MISS_KEY_NOT_FOUND)
            translist_unhit.append(tran)
            LOGGER.debug(f"[cache]message未命中缓存: {line_now}")
            if "rebuild" in eng_type:
                LOGGER.error(f"[cache]message未命中缓存: {line_now}")
            continue

        # 有校对稿就等于有最终稿：既然校对过，原文后来改没改都不再影响这条的译文，
        # 下面那几项检查（post_src / pre_dst / 翻译失败）整段跳过。
        # 取默认 "" 而不是 None：字段整个缺失（很老的缓存、手改过的缓存）应当作"没校对过"，
        # 该走的检查一步都不能少——否则 `None == ""` 是 False，会把这类缓存当成有校对稿，
        # 原文早已改过的旧缓存也照样算命中。
        no_proofread = _cache_get(cache_dict[cache_key], "proofread_dst", "") == ""

        if no_proofread:
            # post_src被改变
            if load_post_src == ignr_post_src == False:
                if tran.post_src != _cache_get(cache_dict[cache_key], "post_src"):
                    _mark_cache_miss(tran, MISS_POST_SRC_CHANGED)
                    translist_unhit.append(tran)
                    LOGGER.debug(f"[cache]post_src被改变: \npost_src_before{_cache_get(cache_dict[cache_key], 'post_src')}\npost_src_now{tran.post_src}")
                    if "rebuild" in eng_type:
                        LOGGER.error(f"[cache]post_src被改变: \npost_src_before: {_cache_get(cache_dict[cache_key], 'post_src')}\npost_src_now: {tran.post_src}")
                    continue
            # pre_dst为空
            if tran.post_src != "":
                if (
                    not _cache_has(cache_dict[cache_key], "pre_dst")
                    or _cache_get(cache_dict[cache_key], "pre_dst") == ""
                ):
                    _mark_cache_miss(tran, MISS_PRE_DST_EMPTY)
                    translist_unhit.append(tran)
                    LOGGER.debug(f"[cache]pre_dst为空: {line_now}")
                    if "rebuild" in eng_type:
                        LOGGER.error(f"[cache]pre_dst为空: {line_now}")
                    continue
            # 重试失败的（走到这里的本来就已经是"没校对稿"的了）
            if (
                retry_failed
                and filter_problem_text("翻译失败", problem_filter_keys)
                and "(Failed)" in _cache_get(cache_dict[cache_key], "pre_dst")
            ):
                _mark_cache_miss(tran, MISS_TRANSLATE_FAILED)
                translist_unhit.append(tran)
                LOGGER.debug(f"[cache]Failed translation: {line_now}")
                if "rebuild" in eng_type:
                    LOGGER.error(f"[cache]Failed translation: {line_now}")
                continue

            # retran_key在pre_src中
            if retran_key and check_retran_key(
                retran_key, _cache_get(cache_dict[cache_key], "pre_src")
            ):
                if "rebuild" not in eng_type:
                    _mark_cache_miss(tran, MISS_RETRAN_KEY)
                    translist_unhit.append(tran)
                    LOGGER.info(f"[cache]retran_key in 'pre_src' message: {line_now}")
                    continue
            # retran_key在problem中
            if retran_key and "problem" in cache_dict[cache_key]:
                if check_retran_key(retran_key, filter_problem_text(cache_dict[cache_key]["problem"], problem_filter_keys)):
                    if "rebuild" not in eng_type:
                        _mark_cache_miss(tran, MISS_RETRAN_PROBLEM)
                        translist_unhit.append(tran)
                        LOGGER.info(f"[cache]retran_key in 'problem' message: {line_now}")
                        continue

        # 击中缓存的,post_dst初始值赋pre_dst
        tran.pre_dst = _cache_get(cache_dict[cache_key], "pre_dst")
        if "trans_by" in cache_dict[cache_key]:
            tran.trans_by = cache_dict[cache_key]["trans_by"]
        if _cache_has(cache_dict[cache_key], "proofread_dst"):
            tran.proofread_zh = _cache_get(cache_dict[cache_key], "proofread_dst")
        if "proofread_by" in cache_dict[cache_key]:
            tran.proofread_by = cache_dict[cache_key]["proofread_by"]
        if "trans_conf" in cache_dict[cache_key]:
            tran.trans_conf = cache_dict[cache_key]["trans_conf"]
        # 校对批注：新键 proofread_comment，旧缓存里是 doub_content（见 _CACHE_KEY_COMPAT）
        if _cache_has(cache_dict[cache_key], "proofread_comment"):
            tran.proofread_comment = _cache_get(cache_dict[cache_key], "proofread_comment") or ""
        if "unknown_proper_noun" in cache_dict[cache_key]:
            tran.unknown_proper_noun = cache_dict[cache_key]["unknown_proper_noun"]
        if "skip_check" in cache_dict[cache_key]:
            tran.skip_check = bool(cache_dict[cache_key]["skip_check"])

        if tran.proofread_zh != "":
            tran.post_dst = tran.proofread_zh
        else:
            tran.post_dst = tran.pre_dst

        # 校对模式下，未校对的
        if proofread and tran.proofread_zh == "":
            _mark_cache_miss(tran, MISS_PROOFREAD_MISSING)
            translist_unhit.append(tran)
            continue

        # 不检查post_src是否被改变, 且直接使用cache的post_src
        if load_post_src:
            tran.post_src = _cache_get(cache_dict[cache_key], "post_src")

        translist_hit.append(tran)

    return translist_hit, translist_unhit


def check_retran_key(retran_key, target):
    """
    检查 retran_key 是否存在于目标字符串中。

    Args:
        retran_key (str or list): 需要检查的关键字，可以是字符串或字符串列表。
        target (str): 目标字符串。

    Returns:
        bool: 如果 retran_key 存在于目标字符串中，返回 True；否则返回 False。
    """
    # 过滤空串/None：空子串恒 `in` 任何字符串，若用户配置里写成 `- ""`
    # 或 `- null`，会导致所有句子被标记为需要重翻。这里防御性跳过。
    if isinstance(retran_key, str):
        return bool(retran_key) and retran_key in target
    elif isinstance(retran_key, list):
        return any(key in target for key in retran_key if key)
    return False
