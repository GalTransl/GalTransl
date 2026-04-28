"""
缓存机制
"""

from GalTransl.CSentense import CTransList
from GalTransl import LOGGER
from typing import List
import orjson
import os,shutil
from GalTransl.i18n import get_text,GT_LANG
import aiofiles

# 缓存JSON key映射：新key -> 旧key（用于兼容读取旧缓存）
_CACHE_KEY_COMPAT = {
    "pre_src": "pre_jp",
    "post_src": "post_jp",
    "pre_dst": "pre_zh",
    "proofread_dst": "proofread_zh",
    "post_dst_preview": "post_zh_preview",
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


_CACHE_APPEND_SUFFIX = ".append.jsonl"


def _append_cache_file_path(cache_file_path: str) -> str:
    return cache_file_path + _CACHE_APPEND_SUFFIX


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

    cache_obj["trans_by"] = tran.trans_by
    cache_obj["proofread_by"] = tran.proofread_by

    if tran.trans_conf != 0:
        cache_obj["trans_conf"] = tran.trans_conf
    if tran.doub_content != "":
        cache_obj["doub_content"] = tran.doub_content
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
    shutil.move(temp_file_path, cache_file_path)

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


async def save_transCache_to_json(trans_list: CTransList, cache_file_path, post_save=False):
    """
    此函数将翻译缓存保存到 JSON 文件中。
    使用原子写入机制，避免程序异常关闭时写入不完整的问题。

    Args:
        trans_list (CTransList): 要保存的翻译列表。
        cache_file_path (str): 要保存到的 JSON 文件的路径。
        post_save (bool, optional): 是否是翻译结束后的存储。默认为 False。
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
            shutil.move(temp_file_path, cache_file_path)
            if os.path.exists(append_file_path):
                os.remove(append_file_path)
        else:
            # 增量写入append日志，避免频繁整文件重写
            if append_entries:
                async with aiofiles.open(append_file_path, mode="ab") as f:
                    for entry in append_entries:
                        await f.write(orjson.dumps(entry))
                        await f.write(b"\n")
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
            translist_unhit.append(tran)
            LOGGER.debug(f"[cache]message未命中缓存: {line_now}")
            if "rebuild" in eng_type:
                LOGGER.error(f"[cache]message未命中缓存: {line_now}")
            continue

        no_proofread = _cache_get(cache_dict[cache_key], "proofread_dst") == ""

        if no_proofread:
            # post_src被改变
            if load_post_src == ignr_post_src == False:
                if tran.post_src != _cache_get(cache_dict[cache_key], "post_src"):
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
                    translist_unhit.append(tran)
                    LOGGER.debug(f"[cache]pre_dst为空: {line_now}")
                    if "rebuild" in eng_type:
                        LOGGER.error(f"[cache]pre_dst为空: {line_now}")
                    continue
            # 重试失败的
            if retry_failed and "(Failed)" in _cache_get(cache_dict[cache_key], "pre_dst"):
                if (
                    no_proofread or "Fail" in cache_dict[cache_key]["proofread_by"]
                ):  # 且未校对
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
                    translist_unhit.append(tran)
                    LOGGER.info(f"[cache]retran_key in 'pre_src' message: {line_now}")
                    continue
            # retran_key在problem中
            if retran_key and "problem" in cache_dict[cache_key]:
                if check_retran_key(retran_key, cache_dict[cache_key]["problem"]):
                    if "rebuild" not in eng_type:
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
        if "doub_content" in cache_dict[cache_key]:
            tran.doub_content = cache_dict[cache_key]["doub_content"]
        if "unknown_proper_noun" in cache_dict[cache_key]:
            tran.unknown_proper_noun = cache_dict[cache_key]["unknown_proper_noun"]

        if tran.proofread_zh != "":
            tran.post_dst = tran.proofread_zh
        else:
            tran.post_dst = tran.pre_dst

        # 校对模式下，未校对的
        if proofread and tran.proofread_zh == "":
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
