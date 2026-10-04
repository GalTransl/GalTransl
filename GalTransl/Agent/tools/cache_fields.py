"""翻译缓存条目的字段定义（读取投影与可 patch 字段）。"""

from __future__ import annotations

from typing import Any


# 缓存条目可选的字段（名字即返回体里的键），与缓存 JSON 的字段一致。
CACHE_ENTRY_FIELDS: tuple[str, ...] = (
    "index",
    "name",
    "pre_src",
    "post_src",
    "pre_dst",
    "post_dst_preview",
    "proofread_dst",
    "proofread_by",
    "proofread_comment",
    "trans_by",
    "problem",
)
# 默认精简集：一条只回「谁说的 + 原文 + 译文 + 问题」。
# 原文只给一列——post_src（真正送去翻译的那版），不再同时带 pre_src：两列在多数条目上
# 只差对话符号/译前字典替换，一次读几十条就是双份原文，白占上下文（要看 pre_src 传 fields）。
# trans_by 同理默认不给（它是"哪个模型翻的"，读译文时基本用不上）。
CACHE_ENTRY_FIELDS_DEFAULT: tuple[str, ...] = (
    "index",
    "name",
    "post_src",
    "pre_dst",
    "problem",
    # 校对子代理的产物（校对批注）：主 Agent 复核时要看它，默认就得带上；没写过则是空值，
    # 走默认精简集的"空值省略"，不会给普通条目添噪音
    "proofread_comment",
)
# 每个字段的含义（拼进 system prompt，见 _cache_fields_section）。
# 命名来源见 GalTransl/CSentense.py：pre_src=前原、post_src=前润（送去翻译的原文）、
# pre_dst=后原（模型原始译文）、post_dst=后润（最终译文）。
CACHE_ENTRY_FIELD_DESCRIPTIONS: dict[str, str] = {
    "index": "条目序号（1 起、按文件顺序）；改译文/删条目/读上下文都用它定位",
    "name": "说话人；旁白为空",
    "pre_src": "原始原文（前原），管道最开始的句子；默认不返回（要看它传 fields）",
    "post_src": "真正送去翻译的原文（前润）：对话符号处理 + 译前字典替换之后的文本",
    "pre_dst": "模型返回的译文（后原），未经译后字典替换",
    "post_dst_preview": "最终译文的缓存快照（后润）：译后字典替换 + 对话符号恢复之后的形态；默认只在它与译文实质不同（不只差首尾对话符号）时返回",
    "proofread_dst": "校对/润色稿；有内容时它就是这条的最终译文（优先于 pre_dst）",
    "proofread_by": "校对者标记（校对失败的会带 Fail）；未校对为空",
    "proofread_comment": "校对批注：校对子代理（run_subagents）的批注：记录未解决或修复后仍需二次审查的事项——校对建议（错译/漏译/事实错误等）或润色建议（翻译腔、口语不自然等表达改进），一条一句；没写过的条目为空。要处理这条就按批注改当前生效的译文字段（proofread_dst 优先，否则 pre_dst），改完在 patch_transl_cache 里带 clear_comment=true 把这些条目的批注一次清空表示已处理（只清某几条就逐条传 proofread_comment 空串）",
    "trans_by": "译者标记：翻译引擎的模型名，或被别的来源改过时的那个名字（本会话 Agent 用 patch_transl_cache 改过的条目记的是 Agent 的模型名）；读缓存时逐条只报少数派——这批里出现最多的那个（多数派，通常就是引擎翻的）与空值都不逐条给，多数派记在顶层 majority_trans_by；默认不返回（要看它传 fields）",
    "problem": "自动问题分析写入的问题标签，可能多条（以「, 」分隔）；list_problems 的统计与下钻都基于它",
}
# 默认模式下"有内容才带上"的附加字段（空值一律省略）
CACHE_ENTRY_FIELDS_IF_PRESENT: tuple[str, ...] = (
    "proofread_dst",
    "proofread_by",
)


# 缓存 JSON 的旧键名（与 GalTransl/Cache.py 的 _CACHE_KEY_COMPAT 一致）：老项目里存的
# 是 pre_jp/post_jp/pre_zh 那一套，读取时按新名取不到，得回退到旧名。
_CACHE_ENTRY_OLD_KEYS: dict[str, str] = {
    "pre_src": "pre_jp",
    "post_src": "post_jp",
    "pre_dst": "pre_zh",
    "proofread_dst": "proofread_zh",
    "post_dst_preview": "post_zh_preview",
    # 校对批注的旧名（只读兼容旧缓存；写回一律用新名，见 GalTransl/Cache.py 的 _CACHE_KEY_COMPAT）
    "proofread_comment": "doub_content",
}


def _cache_field_value(entry: dict[str, Any], name: str) -> Any:
    """按字段名取缓存条目的值，兼容旧缓存的旧键名（取不到返回 None）。"""
    if name in entry:
        return entry[name]
    old_key = _CACHE_ENTRY_OLD_KEYS.get(name)
    return entry.get(old_key) if old_key else None


# patch_transl_cache 允许更新的条目字段白名单（其余字段一律不动，避免误改 problem/preview
# 等派生字段）。trans_by 不在这里：它是"谁改的"标记，由工具自动填成本会话的模型名，
# 不能让模型自己声明（写 "manual" 这种会把"翻译引擎翻的"和"Agent 改的"混起来）。
_PATCHABLE_FIELDS: frozenset[str] = frozenset({
    "pre_dst",
    "proofread_dst",
    # 校对批注也算可改：校对子代理写下意见、主 Agent 改完译文后要能把它清掉（或改写）
    "proofread_comment",
})

# "改了译文"的那两个字段：只有它们被改过才给条目盖 trans_by（谁改的）；只写校对意见不算
_TRANSLATION_FIELDS: frozenset[str] = frozenset({"pre_dst", "proofread_dst"})


def _patchable_fields_text(allowed: frozenset[str] | None = None) -> str:
    """可改字段的一行文本（按 CACHE_ENTRY_FIELDS 的顺序，输出稳定）。

    system prompt 的字段说明与 patch 工具的报错都用它，避免两处各写一份再漂移。
    allowed 可为内部调用提供更窄的字段白名单。
    """
    fields = allowed if allowed is not None else _PATCHABLE_FIELDS
    return " / ".join(name for name in CACHE_ENTRY_FIELDS if name in fields)
