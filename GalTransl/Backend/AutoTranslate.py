"""自动模式：按成功率挑模板和改善拒答方式，按失败原因决定下一步，减少无效重试。

一次尝试的「策略」= 模板（ForGal-tool / markdown / json、ForNovel-tool / ForNovel）
+ 改善拒答方式（none / prefill / image）。

要不要改善拒答按文件区分；用哪个模板、哪种改善拒答方式不分文件，按共用的成功率统计挑。

失败先分类，再对症处理：
- 被拒（输出为空/被拦截/content_filter/回复是拒绝说明）：这个文件之后都带改善拒答，换一个本批
  还没被拒过的「模板 + 改善拒答方式」组合，并缩小批次隔离问题句；
- 格式错（解析不出、缺句、补丁写错）：本批不再用这个模板，换成功率次高的模板，并缩小批次；
- API 错误（网络、限流、5xx）：模板和改善拒答都没问题，保持策略、只换令牌，不缩小批次；
- 模型不支持图片（图片请求被 4xx 拒收）：本次任务停用图片方式。

模板只交回开头连续几句（后面缺句或有坏行）不走上面的重试：这几句已逐句通过校验，直接采用，
剩下的由下一批接着翻；模板照样记一次格式失败，让成功率统计反映出来。

成功率按「文件类型（有无 name）× 模型」分别统计，所有文件共用：模板只记格式成败；是否被拒按
「模板 + 改善拒答方式」的组合记（同样叫预置补全，tool 模板是让模型调用读取工具，json 是预置
回复开头，效果不能混在一起算）。API 错误两边都不计。每个文件只记住自己要不要改善拒答；带着
改善拒答连续成功几批后，只派一个批次试着不带：成功就关掉，被拒就保持，并把下次试的间隔加倍。
"""

from contextvars import ContextVar
from copy import copy
from dataclasses import dataclass
import random
import re

import httpx
from openai import APIError

from GalTransl import LOGGER
from GalTransl.Backend.BaseTranslate import BaseTranslate, TranslationParseError, TranslationRequestError
from GalTransl.Backend.ForGalJsonTranslate import ForGalJsonTranslate
from GalTransl.Backend.ForGalToolTranslate import ForGalToolTranslate
from GalTransl.Backend.ForGalMarkdownTranslate import ForGalMarkdownTranslate
from GalTransl.Backend.ForNovelTranslate import ForNovelTranslate
from GalTransl.Backend.ForNovelToolTranslate import ForNovelToolTranslate
from GalTransl.Backend.Prompts import H_WORDS_LIST
from GalTransl.Backend.Prompts import Sakura_SYSTEM_PROMPT010, Sakura_TRANS_PROMPT010, GalTransl_SYSTEM_PROMPT, GalTransl_TRANS_PROMPT_V3
from GalTransl.Backend.SakuraTranslate import CSakuraTranslate
from GalTransl.Backend.MultiTurnTranslate import JAILBREAK_OVERRIDE, MultiTurnTranslate


class _AutoSakuraTranslate(CSakuraTranslate, MultiTurnTranslate):
    """复用专用模板及解析器，连接自动模式当前选择的 OpenAI 兼容后端。"""

    def __init__(self, config, eng_type, proxy_pool, token_pool):
        BaseTranslate.__init__(self, config, eng_type, proxy_pool, token_pool)
        self.last_translations = {}
        self.system_prompt, self.trans_prompt = (
            (Sakura_SYSTEM_PROMPT010, Sakura_TRANS_PROMPT010)
            if eng_type == "sakura-v1.0"
            else (GalTransl_SYSTEM_PROMPT, GalTransl_TRANS_PROMPT_V3)
        )
        self._apply_internal_prompt_template_overrides()
        BaseTranslate.init_chatbot(self, eng_type, config)
        self.model_name = self.client_list[0][1].model_name if self.client_list else eng_type
        self._set_temp_type("precise")

    async def translate(self, trans_list, gptdict="", proofread=False, filename=""):
        previous = [(row.pre_dst, row.trans_by) for row in trans_list] if proofread else []
        count, translated = await CSakuraTranslate.translate(self, trans_list, gptdict, filename)
        if proofread:
            for result, (pre_dst, trans_by) in zip(translated, previous):
                result.proofread_zh = result.post_dst
                result.proofread_by = result.trans_by
                result.pre_dst, result.trans_by = pre_dst, trans_by
        return count, translated


GAL_MODES = ("ForGal-tool", "ForGal-markdown", "ForGal-json")
NOVEL_MODES = ("ForNovel-tool", "ForNovel")
ENGINE_CLASSES = {
    "ForGal-tool": ForGalToolTranslate,
    "ForGal-markdown": ForGalMarkdownTranslate,
    "ForGal-json": ForGalJsonTranslate,
    "ForNovel": ForNovelTranslate,
    "ForNovel-tool": ForNovelToolTranslate,
    "sakura-v1.0": _AutoSakuraTranslate,
    "galtransl-v3": _AutoSakuraTranslate,
}
MAX_RETRIES = 4

# 改善拒答等级，按开销从低到高
JB_LEVELS = ("none", "prefill", "image")
JB_LABELS = {"none": "不改善拒答", "prefill": "预置补全", "image": "图片输入"}
# 预置补全：json / TSV 预置回复开头；tool 没有可预置的开头，改为让模型先调用读取工具取回
# 前文和输入（见 ForGalToolTranslate._prepare_translation_messages）；markdown 没有固定开头，不支持
PREFILL_MODES = frozenset({"ForGal-tool", "ForGal-json", "ForNovel-tool", "ForNovel"})
IMAGE_MODES = frozenset({"ForGal-tool", "ForGal-markdown", "ForGal-json", "ForNovel-tool", "ForNovel"})

# 先验：没有数据时模板的格式成功率按 0.8 估，强度相当于 4 次观测；之后由实际成败主导
TEMPLATE_PRIOR, TEMPLATE_PRIOR_WEIGHT = 0.8, 4
# 「模板 + 改善拒答方式」组合的抗拒答先验：预置补全和图片取相同先验，开头由开销决定（先试便宜的），
# 之后哪个组合更少被拒哪个优先
JB_PRIOR = {"none": 0.5, "prefill": 0.6, "image": 0.6}
JB_PRIOR_WEIGHT = 2
JB_COST = 0.05          # 每升一级改善拒答扣的分：效果相近时用便宜的
PROBE_AFTER, PROBE_AFTER_MAX = 3, 24  # 带改善拒答连续成功几批后试着不带；试降被拒则间隔加倍

_STRUCTURED_REPLY = re.compile(r'\|\s*\{\s*"id"|^\s*\|.*\|\s*$|\t\d+\s*$', re.M)
_REFUSAL_REPLY = re.compile(
    r"sorry|i can(?:'|no)t|i am unable|i'm unable|unable to (?:help|assist|comply|provide|translate)"
    r"|cannot (?:help|assist|comply|provide|translate|fulfil)|not able to (?:help|assist|provide)"
    r"|could not be submitted|content (?:policy|filter|management)|usage polic|prohibited|violat"
    r"|inappropriate|safety|blocked|抱歉|对不起|无法(?:协助|提供|翻译|完成|处理|继续)|不能(?:协助|提供|翻译)"
    r"|违反|不适当|敏感内容",
    re.I,
)
_REFUSAL_REQUEST = re.compile(
    r"content[_ ]?filter|content[_ ]policy|prohibited|safety|blocked|block_reason|violat|sensitive"
    r"|no_candidates|content is empty|data_inspection|inappropriate",
    re.I,
)
_IMAGE_UNSUPPORTED = re.compile(
    r"image|vision|multi-?modal|image_url|content type|unsupported content|不支持(?:图片|图像|多模态)",
    re.I,
)


def classify_failure(exc, jb="none"):
    """把一次失败归为 refusal / format / image_unsupported / api。"""
    if isinstance(exc, TranslationParseError):
        message = str(exc)
        reply = getattr(exc, "reply", None)
        if "被拦截" in message or "content_filter" in message:
            return "refusal"
        if reply is not None:
            text = reply.split("</think>")[-1].strip()
            if not text:
                return "refusal"
            # 长输出、结构化输出是真译文（只是格式有问题），里面出现「抱歉」不代表拒答
            if len(text) < 600 and not _STRUCTURED_REPLY.search(text) and _REFUSAL_REPLY.search(text):
                return "refusal"
        return "format"
    text = str(exc)
    if _REFUSAL_REQUEST.search(text):
        return "refusal"
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "__cause__", None), "status_code", None)
    if jb == "image" and _IMAGE_UNSUPPORTED.search(text) and (status in (None, 400, 415, 422)):
        return "image_unsupported"
    return "api"


@dataclass
class _Strategy:
    """一个文件（含它被拆出的并发片段）要不要改善拒答。

    用哪个模板、哪种改善拒答方式不记在这里：那是所有文件共用的统计（见 _choose）。
    """
    need: bool = False      # 这个文件被拒过：之后的批次都带改善拒答
    streak: int = 0         # 带着改善拒答连续成功的批数
    probe_after: int = PROBE_AFTER
    probe_pending: bool = False   # 下一批试着不带改善拒答
    probe_inflight: bool = False  # 已有批次在试降，其余批次照常带改善拒答


class AutoTranslate(BaseTranslate):
    def __init__(self, config, eng_type, proxy_pool, token_pool):
        super().__init__(config, eng_type, proxy_pool, token_pool)
        self._proxy_pool, self._token_pool = proxy_pool, token_pool
        self._engines = {}
        self._preferred_tokens = {}
        client_list = getattr(self, "client_list", [])
        if getattr(self, "tokenStrategy", "random") == "fallback":
            self._active_token_index = 0
        else:
            self._active_token_index = (
                random.randrange(len(client_list)) if client_list else 0
            )
        self._batch_scope = ContextVar("auto_translation_batch", default=None)
        backend = config.getBackendConfigSection("OpenAI-Compatible")
        model_name = backend.get("rewriteModelName", "")
        if not model_name and token_pool is not None:
            tokens = token_pool.get_available_token()
            model_name = tokens[0].model_name if tokens else ""
        model_name = str(model_name).lower()
        self._special_mode = (
            "sakura-v1.0" if "sakura" in model_name
            else "galtransl-v3" if "galtransl" in model_name else None
        )

    # ---- 状态（延迟初始化：测试替身和旧插件可能跳过 __init__） ----

    def _state(self):
        if not hasattr(self, "_auto_state"):
            self._auto_state = {"strategies": {}, "template": {}, "jailbreak": {}, "image_disabled": False}
        return self._auto_state

    def _jb_config(self):
        """(是否常开改善拒答, 是否允许图片方式)。图片需要视觉模型，只在用户选了 image 时才用。"""
        get_key = getattr(getattr(self, "pj_config", None), "getKey", None)
        if not callable(get_key):
            return False, False
        always_on = self._coerce_bool(get_key("gpt.enhance_jailbreak", False))
        mode = str(get_key("gpt.enhance_jailbreak.mode", "prefill") or "").strip().lower()
        return always_on, mode == "image"

    def _jb_supported(self, mode, level):
        jb = JB_LEVELS[level]
        if jb == "none":
            return True
        if jb == "prefill":
            return mode in PREFILL_MODES
        return mode in IMAGE_MODES and self._jb_config()[1] and not self._state()["image_disabled"]

    def _levels(self, modes):
        return [level for level in range(len(JB_LEVELS)) if any(self._jb_supported(m, level) for m in modes)]

    def _base_level(self, modes):
        """常开改善拒答时的最低等级：用户选的方式可用就从它开始，否则取能用的最低一级。"""
        always_on, image_allowed = self._jb_config()
        if not always_on:
            return 0
        levels = self._levels(modes)
        preferred = JB_LEVELS.index("image" if image_allowed else "prefill")
        if preferred in levels:
            return preferred
        raised = [level for level in levels if level > 0]
        return raised[0] if raised else 0

    def _model_name(self, token_index):
        client_list = getattr(self, "client_list", [])
        if token_index < len(client_list):
            return getattr(client_list[token_index][1], "model_name", "")
        return ""

    @staticmethod
    def _rate(counts, prior, weight):
        ok, bad = counts
        return (ok + prior * weight) / (ok + bad + weight)

    def _template_score(self, group, model, mode):
        counts = self._state()["template"].get((group, model, mode), (0, 0))
        return self._rate(counts, TEMPLATE_PRIOR, TEMPLATE_PRIOR_WEIGHT)

    def _jb_score(self, group, model, mode, level):
        jb = JB_LEVELS[level]
        counts = self._state()["jailbreak"].get((group, model, mode, jb), (0, 0))
        return self._rate(counts, JB_PRIOR[jb], JB_PRIOR_WEIGHT)

    def _count(self, table, key, ok):
        counts = self._state()[table].get(key, (0, 0))
        self._state()[table][key] = (counts[0] + 1, counts[1]) if ok else (counts[0], counts[1] + 1)

    def _strategy(self, file_key, modes):
        strategy = self._state()["strategies"].setdefault(file_key, _Strategy())
        if self._base_level(modes) > 0:
            strategy.probe_pending = False  # 常开改善拒答：不试降
        return strategy

    def _claim_need(self, strategy, modes):
        """本次尝试要不要带改善拒答；有待试降且没人在试时，由这次尝试去试。返回 (要不要, 是否试降)。"""
        if strategy.probe_pending and not strategy.probe_inflight:
            strategy.probe_inflight = True
            return False, True
        return strategy.need or self._base_level(modes) > 0, False

    @staticmethod
    def _end_probe(strategy, refused):
        strategy.probe_inflight = False
        strategy.probe_pending = False
        strategy.streak = 0
        if refused:
            strategy.probe_after = min(strategy.probe_after * 2, PROBE_AFTER_MAX)

    def _choose(self, modes, group, model, need, excluded, refused):
        """在本批没出过格式错误的模板里挑（模板, 改善拒答等级）。

        文件不需要改善拒答时只比模板的格式成功率。需要时在「模板 + 改善拒答方式」的组合里挑，
        比「格式成功率 × 未被拒率」，效果相近时用便宜的；本批已被拒过的组合先不再用，
        全都被拒过就重新从头挑。常开改善拒答时不低于用户选的方式（见 _base_level）。
        """
        floor = max(1, self._base_level(modes)) if need else 0
        best = fallback = None
        skipped_refused = False
        for order, mode in enumerate(modes):
            if mode in excluded:
                continue
            template = self._template_score(group, model, mode)
            tie = 0.001 * order  # 同分时保持默认顺序
            if not need:
                levels = [0]
            else:
                levels = [lv for lv in range(floor, len(JB_LEVELS)) if self._jb_supported(mode, lv)]
            if not levels:
                # 这个模板没有可用的改善拒答方式：只在没有别的选择时不带改善拒答地用
                if fallback is None or template - tie > fallback[0]:
                    fallback = (template - tie, mode, 0)
                continue
            for lv in levels:
                if (mode, lv) in refused:
                    skipped_refused = True
                    continue
                score = template - tie
                if need:
                    score = template * self._jb_score(group, model, mode, lv) - JB_COST * lv - tie
                if best is None or score > best[0]:
                    best = (score, mode, lv)
        if best is None and skipped_refused:
            refused.clear()
            return self._choose(modes, group, model, need, excluded, refused)
        best = best or fallback
        if best is None:
            excluded.clear()  # 全都失败过：本批重新从头挑
            return self._choose(modes, group, model, need, excluded, refused)
        return best[1], best[2]

    def _after_success(self, strategy, modes, used, probing, label):
        if probing:
            self._end_probe(strategy, refused=False)
            if strategy.need:
                LOGGER.info("[自动策略][%s] 试降成功，本文件之后不再改善拒答", label)
            strategy.need = False
            strategy.probe_after = PROBE_AFTER
            return
        # 并发时，早于被拒发出的不带改善拒答的请求成功不算数；只统计带着改善拒答的连续成功
        if not strategy.need or self._base_level(modes) > 0 or used == 0:
            if not strategy.need:
                strategy.streak = 0
            return
        strategy.streak += 1
        if strategy.streak < strategy.probe_after or strategy.probe_pending:
            return
        LOGGER.info(
            "[自动策略][%s] 改善拒答已连续成功 %s 批，派下一批试着降回 %s 以节省开销",
            label, strategy.streak, JB_LABELS["none"],
        )
        strategy.probe_pending = True

    def _get_engine(self, mode):
        if mode not in self._engines:
            # 每个格式使用自己的提示词，不能把 tool 的覆盖提示套到 TSV/JSON。
            config = copy(self.pj_config)
            config.keyValues = dict(self.pj_config.keyValues)
            for field in ("system_prompt", "user_prompt"):
                key = f"internals.prompt_template.{field}_override"
                config.keyValues.pop(key, None)
                override = self.pj_config.getKey("internals.auto_translate.prompt_overrides", {}).get(mode, {})
                if isinstance(override.get(field), str):
                    config.keyValues[key] = override[field]
            engine = ENGINE_CLASSES[mode](config, mode, self._proxy_pool, self._token_pool)
            engine.fail_fast = True
            engine.max_api_retries = 1
            self._engines[mode] = engine
        return self._engines[mode]

    def _get_effective_num_per_request(self, configured_value, proofread=False):
        return self._coerce_positive_int(configured_value, 1)

    def _update_dynamic_num_per_request(self, **kwargs):
        # 初始批次遵循设置；错误拆分由本类统一处理。
        pass

    async def batch_translate(
        self, filename, cache_file_path, trans_list, num_pre_request,
        retry_failed=False, gpt_dic=None, proofread=False, retran_key="",
        translist_hit=None, translist_unhit=None,
    ):
        scope = self._batch_scope.set({"contexts": {}, "gpt_dic": gpt_dic})
        try:
            return await self._batch_translate_common(
                filename=filename, cache_file_path=cache_file_path,
                translist_unhit=translist_unhit if translist_unhit is not None else trans_list,
                num_pre_request=num_pre_request, proofread=proofread,
                failed_markers=("(Failed)", "(翻译失败)"), h_words_list=H_WORDS_LIST,
            )
        finally:
            self._batch_scope.reset(scope)

    def _next_token(self, file_key, token_index, num_tokens, client_list):
        """失败后为该文件换一个令牌（多令牌时），返回日志用的说明。"""
        if num_tokens <= 1:
            return ""
        if self._preferred_tokens.get(file_key, 0) == token_index:
            if getattr(self, "tokenStrategy", "random") == "fallback":
                next_token_index = (token_index + 1) % num_tokens
            else:  # "random"
                next_token_index = random.choice([i for i in range(num_tokens) if i != token_index])
            self._preferred_tokens[file_key] = next_token_index
            self._active_token_index = next_token_index
        else:
            next_token_index = self._preferred_tokens.get(file_key, 0)
        if next_token_index < len(client_list):
            return f"，切换令牌为 [{client_list[next_token_index][1].maskToken()}]"
        return ""

    async def translate(self, trans_list, gptdict="", proofread=False, filename=""):
        if not trans_list:
            return 0, []
        has_name = any(getattr(tran, "source_file_has_name", bool(tran.speaker)) for tran in trans_list)
        modes = GAL_MODES if has_name else NOVEL_MODES
        group = "gal" if has_name else "novel"
        if getattr(self, "_special_mode", None):
            modes = (self._special_mode,)
            group = self._special_mode
        file_key = (getattr(trans_list[0], "source_file_path", filename), has_name)
        state = self._batch_scope.get() or {"contexts": {}, "gpt_dic": None}
        current = trans_list
        if not hasattr(self, "_preferred_tokens"):
            self._preferred_tokens = {}

        client_list = getattr(self, "client_list", [])
        num_tokens = len(client_list)
        if file_key not in self._preferred_tokens:
            if getattr(self, "tokenStrategy", "random") == "fallback":
                self._preferred_tokens[file_key] = (
                    getattr(self, "_active_token_index", 0) % num_tokens if num_tokens > 0 else 0
                )
            else:
                self._preferred_tokens[file_key] = (
                    random.randrange(num_tokens) if num_tokens > 0 else 0
                )

        excluded = set()  # 本批格式出错过的模板
        refused = set()   # 本批被拒过的（模板, 改善拒答等级）
        for attempt in range(MAX_RETRIES + 1):
            self._check_stop_requested()
            token_index = (
                self._preferred_tokens.get(file_key, 0) % num_tokens if num_tokens > 0 else 0
            )
            model = self._model_name(token_index)
            strategy = self._strategy(file_key, modes)
            need, probing = self._claim_need(strategy, modes)
            mode, used = self._choose(modes, group, model, need, excluded, refused)
            jb = JB_LEVELS[used]
            engine = self._get_engine(mode)
            engine._active_token_index = token_index
            label = f"{filename}:{self._build_idx_tip(current)}"
            plan = mode if jb == "none" else f"{mode}+{JB_LABELS[jb]}"
            LOGGER.info(
                "[auto-translate][%s] %s%s，尝试 %s/%s",
                label, plan, "（试降）" if probing else "", attempt + 1, MAX_RETRIES + 1,
            )
            context = state["contexts"].setdefault(mode, {"sessions": {}, "context": {}})
            session_scope = engine._session_scope()
            session_token = session_scope.set(context)
            jb_token = JAILBREAK_OVERRIDE.set("" if jb == "none" else jb)
            # 在副本上翻：模板抛错时，它写了一半的内容和失败标记不能留在正式句子上；
            # 只有模板正常交回的句子（整批，或通过校验的开头几句）才写回。
            working = [copy(tran) for tran in current]
            for i, tran in enumerate(working):
                if i:
                    tran.prev_tran = working[i - 1]
                if i + 1 < len(working):
                    tran.next_tran = working[i + 1]
            try:
                glossary = gptdict
                if state["gpt_dic"] is not None:
                    style = "gpt" if mode in ("ForGal-tool", "ForNovel-tool", "ForGal-json") else "tsv"
                    if mode in ("sakura-v1.0", "galtransl-v3"):
                        style = "sakura"
                    glossary = state["gpt_dic"].gen_prompt(current, style)
                count, translated = await engine.translate(working, glossary, proofread=proofread, filename=filename)
                # 部分结果：模板只交回了开头连续的几句（后面缺句或有坏行）。这几句逐句通过了校验，
                # 直接采用，剩下的由下一批接着翻；模板照样记一次格式失败。
                partial = 0 < count < len(current) and len(translated) == count
                if not partial and (count != len(current) or len(translated) != len(current)):
                    raise TranslationParseError(f"译文缺句：{count}/{len(current)}")
                for original, result in zip(current, translated):
                    original.__dict__.update({key: value for key, value in result.__dict__.items() if key not in ("prev_tran", "next_tran")})
                if partial:
                    self._count("template", (group, model, mode), False)
                    engine.reset_conversation(filename)
                    detail = f"只完成 {count}/{len(current)} 句，已采用这 {count} 句，其余下一批重翻；记一次模板失败"
                    LOGGER.warning("[auto-translate][%s] %s %s", label, plan, detail)
                    try:
                        from GalTransl.server import record_runtime_error

                        record_runtime_error(
                            getattr(self.pj_config, "runtime_project_dir", None) or self.pj_config.getProjectDir(),
                            kind="parse",
                            message=f"[auto-translate][{plan}] {detail}",
                            filename=filename,
                            index_range=self._build_idx_tip(current),
                            retry_count=attempt,
                            model=engine._get_chatbot_state()[1],
                            sleep_seconds=0.0,
                            level="warning",
                        )
                    except Exception:
                        LOGGER.debug("自动翻译错误写入运行时记录失败", exc_info=True)
                    return count, list(current[:count])
                # 会话连续性按正式句子对象判断，替换临时副本引用。
                for session in context["sessions"].values():
                    if session.last_tran is working[-1]:
                        session.last_tran = current[-1]
                self._active_token_index = token_index
                self._count("template", (group, model, mode), True)
                self._count("jailbreak", (group, model, mode, jb), True)
                self._after_success(strategy, modes, used, probing, label)
                probing = False
                return count, list(current)
            except (TranslationParseError, TranslationRequestError, APIError, httpx.HTTPError, OSError, ValueError) as exc:
                engine.reset_conversation(filename)
                kind = classify_failure(exc, jb)
                shrink = kind in ("refusal", "format")
                if kind == "format":
                    self._count("template", (group, model, mode), False)
                    excluded.add(mode)
                    following, _ = self._choose(
                        modes, group, model, strategy.need or self._base_level(modes) > 0, set(excluded), set(refused),
                    )
                    action = f"格式错误，切换 {following}" if following != mode else f"格式错误，保持 {mode}"
                elif kind == "refusal" and probing:
                    # 试降被拒说明这个文件还需要改善拒答，不是这批句子的问题：恢复，不缩小批次
                    self._count("jailbreak", (group, model, mode, jb), False)
                    self._end_probe(strategy, refused=True)
                    probing = False
                    shrink = False
                    action = "试降后被拒，恢复改善拒答"
                    LOGGER.info(
                        "[自动策略][%s] 试降后被拒，保持改善拒答，连续成功 %s 批后再试",
                        label, strategy.probe_after,
                    )
                elif kind == "refusal":
                    self._count("jailbreak", (group, model, mode, jb), False)
                    refused.add((mode, used))
                    strategy.streak = 0
                    if not strategy.probe_inflight:
                        strategy.probe_pending = False
                    if not strategy.need:
                        strategy.need = True
                        if self._base_level(modes) == 0:
                            LOGGER.info("[自动策略][%s] 疑似拒答，本文件之后开启改善拒答", label)
                    next_mode, next_used = self._choose(modes, group, model, True, set(excluded), set(refused))
                    if (next_mode, next_used) in refused:
                        action = "疑似拒答，可用的改善拒答方式都已被拒，缩小批次隔离问题句"
                    else:
                        next_jb = JB_LEVELS[next_used]
                        action = "疑似拒答，改用 " + (next_mode if next_jb == "none" else f"{next_mode}+{JB_LABELS[next_jb]}")
                elif kind == "image_unsupported":
                    self._state()["image_disabled"] = True
                    action = "模型疑似不支持图片输入，本次任务停用图片方式"
                    LOGGER.warning("[自动策略] %s：%s", action, exc)
                else:
                    action = f"API 错误，保持 {plan}"
                if probing:
                    # 试降因其它原因失败（格式、API）：还没有结论，留给下一次尝试接着试
                    strategy.probe_inflight = False
                    probing = False
                token_action = self._next_token(file_key, token_index, num_tokens, client_list)
                retry_size = max(1, len(current) // 2) if shrink else len(current)
                exhausted = attempt == MAX_RETRIES
                action_desc = f"{action}{token_action}"
                detail = (
                    "共 5 次请求（4 次重试）仍失败，标记翻译失败"
                    if exhausted else f"{action_desc}，以 {retry_size} 句重试（{attempt + 1}/4）"
                )
                try:
                    from GalTransl.server import record_runtime_error

                    record_runtime_error(
                        getattr(self.pj_config, "runtime_project_dir", None) or self.pj_config.getProjectDir(),
                        kind="parse" if isinstance(exc, (TranslationParseError, ValueError)) else "api",
                        message=f"[auto-translate][{plan}] {type(exc).__name__}: {exc}；{detail}",
                        filename=filename,
                        index_range=self._build_idx_tip(current),
                        retry_count=attempt,
                        model=engine._get_chatbot_state()[1],
                        sleep_seconds=0.0 if exhausted else 1.0,
                        level="error" if exhausted else "warning",
                    )
                except Exception:
                    LOGGER.debug("自动翻译错误写入运行时记录失败", exc_info=True)
                if exhausted:
                    LOGGER.error("[auto-translate][%s] 共 5 次请求（4 次重试）仍失败：%s", label, exc)
                    failed = []
                    self._append_parse_failure_fallback_results(
                        current, 0, failed, mode, proofread=proofread,
                        translate_failed_prefix="(Failed)", translate_problem_message="翻译失败",
                    )
                    return len(failed), failed
                current = current[:retry_size]
                LOGGER.warning("[auto-translate][%s] %s 出错：%s；%s，以 %s 句重试（%s/4）", label, plan, exc, action_desc, len(current), attempt + 1)
                await self._interruptible_sleep(1)
            finally:
                if probing:  # 取消等情况：放开试降名额
                    strategy.probe_inflight = False
                JAILBREAK_OVERRIDE.reset(jb_token)
                session_scope.reset(session_token)

    def strategy_summary(self):
        """本次任务的成功率统计，一行一个模型。"""
        state = self._state()
        lines = []
        models = sorted({key[:2] for key in state["template"]} | {key[:2] for key in state["jailbreak"]})
        for group, model in models:
            parts = []
            for (g, m, mode), (ok, bad) in sorted(state["template"].items()):
                if (g, m) == (group, model):
                    parts.append(f"{mode} {ok}/{ok + bad}")
            for (g, m, mode, jb), (ok, bad) in sorted(state["jailbreak"].items()):
                if (g, m) == (group, model) and (jb != "none" or bad):
                    parts.append(f"{mode}+{JB_LABELS[jb]}未被拒 {ok}/{ok + bad}")
            lines.append(f"[自动策略] 统计（{group}，{model or '默认模型'}）：" + "；".join(parts))
        return lines

    async def shutdown(self):
        try:
            for line in self.strategy_summary():
                LOGGER.info(line)
        except Exception:
            LOGGER.debug("自动策略统计输出失败", exc_info=True)
        try:
            for engine in self._engines.values():
                await engine.shutdown()
        finally:
            self._engines.clear()
            if hasattr(self, "_auto_state"):
                self._auto_state["strategies"].clear()
            if hasattr(self, "_preferred_tokens"):
                self._preferred_tokens.clear()
            await super().shutdown()
