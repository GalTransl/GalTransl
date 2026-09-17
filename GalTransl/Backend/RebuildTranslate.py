from GalTransl.CSentense import *
from GalTransl.ConfigHelper import CProjectConfig
from GalTransl.Dictionary import CGptDict
from GalTransl.Backend.BaseTranslate import BaseTranslate
from GalTransl.Cache import (
    MISS_KEY_NOT_FOUND,
    MISS_POST_SRC_CHANGED,
    MISS_PRE_DST_EMPTY,
    MISS_PROOFREAD_MISSING,
    MISS_RETRAN_KEY,
    MISS_RETRAN_PROBLEM,
    MISS_TRANSLATE_FAILED,
)
from GalTransl import LOGGER
from GalTransl.i18n import get_text,GT_LANG

# 原因码（见 Cache.MISS_*）→ i18n 文案。不在表里的（将来新增的原因码）走 unknown 兜底，
# 带上原因码本身，至少不会是一片空白。
_MISS_REASON_KEYS = {
    MISS_KEY_NOT_FOUND: "cache_incomplete_miss_key_not_found",
    MISS_POST_SRC_CHANGED: "cache_incomplete_miss_post_src_changed",
    MISS_PRE_DST_EMPTY: "cache_incomplete_miss_pre_dst_empty",
    MISS_TRANSLATE_FAILED: "cache_incomplete_miss_translate_failed",
    MISS_RETRAN_KEY: "cache_incomplete_miss_retran_key",
    MISS_RETRAN_PROBLEM: "cache_incomplete_miss_retran_problem",
    MISS_PROOFREAD_MISSING: "cache_incomplete_miss_proofread_missing",
}

_MISS_EXAMPLE_MAX = 3  # 每组最多举几个例子（数量由分组标题给，例子多了只是噪音）
_MISS_EXAMPLE_SOURCE_MAX = 24  # 例句原文的截断长度


def _miss_examples(trans: CTransList) -> str:
    """给一组未命中的句子配一行例子：`例：#33「こんにちは」、#34「おはよう」`。

    带 index 是为了能直接去 read_transl_cache / patch_transl_cache 定位——只说"3 句没命中"
    等于让用户自己翻整个文件。
    """
    parts: list[str] = []
    for tran in trans[:_MISS_EXAMPLE_MAX]:
        source = (tran.pre_src or "").replace("\n", " ").strip()
        if len(source) > _MISS_EXAMPLE_SOURCE_MAX:
            source = source[:_MISS_EXAMPLE_SOURCE_MAX] + "…"
        parts.append(f"#{tran.index}「{source}」")
    return get_text("cache_incomplete_examples", GT_LANG, "、".join(parts)) if parts else ""


def _incomplete_message(filename: str, trans_list: CTransList, translist_unhit: CTransList) -> str:
    """把"缓存不完整"讲清楚：哪几句、为什么没命中、接下来怎么办。

    重建（rebuilda/rebuildr）不翻译，只能用现有缓存重刷译文与结果，所以只要有未命中就得
    失败；但只丢一句「xxx 缓存不完整」的话，用户和 Agent 都不知道该动哪里——最常见的一种
    （改过译前字典 → post_src 变、整批缓存过期）还会被误读成"这个文件还没翻"。
    未命中原因由 Cache.get_transCache_from_json 写在 tran.cache_miss_reason 上，这里按原因
    分组报出来。
    """
    lines = [
        get_text("cache_incomplete", GT_LANG, filename),
        get_text("cache_incomplete_detail", GT_LANG, len(trans_list), len(translist_unhit)),
    ]
    grouped: dict[str, list] = {}
    for tran in translist_unhit:
        reason = getattr(tran, "cache_miss_reason", "") or MISS_KEY_NOT_FOUND
        grouped.setdefault(reason, []).append(tran)
    for reason, trans in grouped.items():
        key = _MISS_REASON_KEYS.get(reason)
        text = (
            get_text(key, GT_LANG)
            if key
            else get_text("cache_incomplete_miss_unknown", GT_LANG, reason)
        )
        lines.append(get_text("cache_incomplete_reason_line", GT_LANG, len(trans), text))
        examples = _miss_examples(trans)
        if examples:
            lines.append("  " + examples)
    lines.append(get_text("cache_incomplete_hint", GT_LANG))
    return "\n".join(lines)


class CRebuildTranslate(BaseTranslate):
    def __init__(
        self,
        config: CProjectConfig,
        eng_type: str,
    ):
        pass

    def init(self) -> bool:
        """
        call it before jobs
        """
        pass

    async def asyncTranslate(self, content: CTransList, gptdict="") -> CTransList:
        """
        translate with async requests
        """
        pass

    async def batch_translate(
        self,
        filename,
        cache_path,
        trans_list: CTransList,
        num_pre_req: int,
        retry_failed: bool = False,
        gpt_dic: CGptDict = None,
        proofread: bool = False,
        retran_key: str = "",
        translist_hit: CTransList = [],
        translist_unhit: CTransList = [],
    ) -> CTransList:

        if len(translist_hit) != len(trans_list):  # 不Build
            error_msg = _incomplete_message(filename, trans_list, translist_unhit)
            LOGGER.error(error_msg)
            raise Exception(error_msg)

        return translist_hit
