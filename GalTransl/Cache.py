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

    # 创建临时文件路径，用于原子写入
    temp_file_path = cache_file_path + ".tmp"

    cache_json = []

    for tran in trans_list:
        if tran.post_jp == "": # 对齐get逻辑
            continue
        if tran.pre_zh == "":
            continue


        cache_obj = {
            "index": tran.index,
            "name": tran.speaker,
            "pre_src": tran.pre_jp,
            "post_src": tran.post_jp,
            "pre_dst": tran.pre_zh,
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
            cache_obj["post_dst_preview"] = tran.post_zh
        cache_json.append(cache_obj)

    try:
        # 原子写入：先写入临时文件
        async with aiofiles.open(temp_file_path, mode="wb") as f:
            json_data = orjson.dumps(cache_json, option=orjson.OPT_INDENT_2)
            await f.write(json_data)
        
        # 原子操作：重命名临时文件为目标文件
        shutil.move(temp_file_path, cache_file_path)
        #LOGGER.debug(f"[cache]缓存已安全保存到：{cache_file_path}")
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
    load_post_jp=False,
    ignr_post_jp=False,
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
        load_post_jp (bool, optional): 不检查post_jp是否被改变, 且直接使用cache的post_jp。默认为 False。
        ignr_post_jp (bool, optional): 仅不检查post_jp是否被改变。默认为 False。

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
                raise e


    for tran in trans_list:
        # 忽略jp为空的句子
        if tran.pre_jp == "" or tran.post_jp == "":
            tran.pre_zh, tran.post_zh = "", ""
            translist_hit.append(tran)
            continue
        # 忽略在读取缓存前pre_zh就有值的句子
        if tran.pre_zh != "":
            tran.post_zh = tran.pre_zh
            translist_hit.append(tran)
            continue

        line_now, line_priv, line_next = "", "None", "None"
        line_now = f"{tran.speaker}{tran.pre_jp}"
        prev_tran = tran.prev_tran
        # 找非空前句
        while prev_tran and prev_tran.post_jp == "":
            prev_tran = prev_tran.prev_tran
        if prev_tran:
            line_priv = f"{prev_tran.speaker}{prev_tran.pre_jp}"
        # 找非空后句
        next_tran = tran.next_tran
        while next_tran and next_tran.post_jp == "":
            next_tran = next_tran.next_tran
        if next_tran:
            line_next = f"{next_tran.speaker}{next_tran.pre_jp}"

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
            if load_post_jp == ignr_post_jp == False:
                if tran.post_jp != _cache_get(cache_dict[cache_key], "post_src"):
                    translist_unhit.append(tran)
                    LOGGER.debug(f"[cache]post_src被改变: \npost_src_before{_cache_get(cache_dict[cache_key], 'post_src')}\npost_src_now{tran.post_jp}")
                    if "rebuild" in eng_type:
                        LOGGER.error(f"[cache]post_src被改变: \npost_src_before: {_cache_get(cache_dict[cache_key], 'post_src')}\npost_src_now: {tran.post_jp}")
                    continue
            # pre_dst为空
            if tran.post_jp != "":
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

        # 击中缓存的,post_zh初始值赋pre_dst
        tran.pre_zh = _cache_get(cache_dict[cache_key], "pre_dst")
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
            tran.post_zh = tran.proofread_zh
        else:
            tran.post_zh = tran.pre_zh

        # 校对模式下，未校对的
        if proofread and tran.proofread_zh == "":
            translist_unhit.append(tran)
            continue

        # 不检查post_src是否被改变, 且直接使用cache的post_src
        if load_post_jp:
            tran.post_jp = _cache_get(cache_dict[cache_key], "post_src")

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
    if isinstance(retran_key, str):
        return retran_key in target
    elif isinstance(retran_key, list):
        return any(key in target for key in retran_key)
    return False
