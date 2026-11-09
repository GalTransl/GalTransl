"""自动模式：按成功率挑模板和改善拒答方式，按失败原因决定下一步，减少无效重试。

一次尝试的「策略」= 模板（ForGal-tool / markdown / json、ForNovel-tool / ForNovel）
+ 改善拒答方式（none / prefill / image）。

失败先分类，再对症处理：
- 被拒（输出为空/被拦截/content_filter/回复是拒绝说明）：换模板没用，升级改善拒答方式，并缩小批次隔离问题句；
- 格式错（解析不出、缺句、补丁写错）：本批不再用这个模板，换成功率次高的模板，并缩小批次；
- API 错误（网络、限流、5xx）：模板和改善拒答都没问题，保持策略、只换令牌，不缩小批次；
- 模型不支持图片（图片请求被 4xx 拒收）：本次任务停用图片方式。

成功率按「文件类型（有无 name）× 模型」分别统计：模板只记格式成败，改善拒答方式只记是否被拒，
API 错误两边都不计。每个文件还会记住自己上次成功的模板和需要的改善拒答等级；在高等级上
连续成功几批后，只派一个批次试着降回更便宜的方式：成功就降级，被拒就保持，并把下次试降的
间隔加倍。
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
# 预置补全只对能预置回复开头的模板有效；tool 走工具调用、markdown 没有固定开头
PREFILL_MODES = frozenset({"ForGal-json", "ForNovel"})
IMAGE_MODES = frozenset({"ForGal-tool", "ForGal-markdown", "ForGal-json", "ForNovel-tool", "ForNovel"})

# 先验：没有数据时模板的格式成功率按 0.8 估，强度相当于 4 次观测；之后由实际成败主导
TEMPLATE_PRIOR, TEMPLATE_PRIOR_WEIGHT = 0.8, 4
# 改善拒答方式的抗拒答先验：预置补全和图片取相同先验，开头由开销决定（先试便宜的），
# 之后谁更少被拒谁优先
JB_PRIOR = {"none": 0.5, "prefill": 0.6, "image": 0.6}
JB_PRIOR_WEIGHT = 2
JB_COST = 0.05          # 每升一级改善拒答扣的分：效果相近时用便宜的
STICKY_BONUS = 0.1      # 文件上次成功的模板加分，避免来回切换
UNSATISFIED_PENALTY = 0.5  # 模板达不到所需改善拒答等级时扣的分
PROBE_AFTER, PROBE_AFTER_MAX = 3, 24  # 高等级上连续成功几批后试降一级；试降失败则间隔加倍

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
    """一个文件（含它被拆出的并发片段）的当前策略。"""
    mode: str = ""          # 上次成功的模板
    level: int = -1         # 需要的改善拒答等级（JB_LEVELS 下标），-1 = 还没初始化
    streak: int = 0         # 在当前（高于基础）等级上连续成功的批数
    probe_after: int = PROBE_AFTER
    probe_level: int = -1   # 待试降到的等级，-1 = 没有待试降
    probe_inflight: bool = False  # 已有批次在试降，其余批次照常用 level


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

    def _jb_score(self, group, model, level):
        jb = JB_LEVELS[level]
        counts = self._state()["jailbreak"].get((group, model, jb), (0, 0))
        return self._rate(counts, JB_PRIOR[jb], JB_PRIOR_WEIGHT)

    def _count(self, table, key, ok):
        counts = self._state()[table].get(key, (0, 0))
        self._state()[table][key] = (counts[0] + 1, counts[1]) if ok else (counts[0], counts[1] + 1)

    def _strategy(self, file_key, modes):
        strategies = self._state()["strategies"]
        strategy = strategies.setdefault(file_key, _Strategy())
        base = self._base_level(modes)
        available = self._levels(modes)
        if strategy.level < 0:
            strategy.level = base
        elif strategy.level < base or strategy.level not in available:
            # 原等级不可用了（如图片方式被停用）：优先就近往上，没有就取能用的最高一级
            higher = [level for level in available if level >= max(base, strategy.level)]
            usable = [level for level in available if level >= base]
            strategy.level = higher[0] if higher else (usable[-1] if usable else base)
        if strategy.probe_level >= 0 and not (
            strategy.probe_level in available and base <= strategy.probe_level < strategy.level
        ):
            strategy.probe_level = -1
        return strategy

    @staticmethod
    def _claim_level(strategy):
        """本次尝试用的等级；有待试降且没人在试时，由这次尝试去试。返回 (等级, 是否试降)。"""
        if strategy.probe_level >= 0 and not strategy.probe_inflight:
            strategy.probe_inflight = True
            return strategy.probe_level, True
        return strategy.level, False

    @staticmethod
    def _end_probe(strategy, refused):
        strategy.probe_inflight = False
        if refused:
            strategy.probe_level = -1
            strategy.streak = 0
            strategy.probe_after = min(strategy.probe_after * 2, PROBE_AFTER_MAX)

    def _choose(self, modes, group, model, level, sticky, excluded):
        """在本批没失败过的模板里挑（模板, 改善拒答等级）。"""
        best = None
        for order, mode in enumerate(modes):
            if mode in excluded:
                continue
            supported = [lv for lv in range(len(JB_LEVELS)) if self._jb_supported(mode, lv)]
            enough = [lv for lv in supported if lv >= level]
            used = min(enough) if enough else max(supported)
            score = (
                self._template_score(group, model, mode)
                - (0 if enough else UNSATISFIED_PENALTY)
                - JB_COST * used
                + (STICKY_BONUS if mode == sticky else 0)
                - 0.001 * order  # 同分时保持默认顺序
            )
            if best is None or score > best[0]:
                best = (score, mode, used)
        if best is None:
            excluded.clear()  # 全都失败过：本批重新从头挑
            return self._choose(modes, group, model, level, sticky, excluded)
        return best[1], best[2]

    def _stronger_level(self, modes, group, model, level):
        """被拒后升到哪一级：只看比当前更强的方式，按过往抗拒答成功率挑，效果相近时用便宜的。"""
        candidates = [lv for lv in self._levels(modes) if lv > level]
        if not candidates:
            return None
        return max(candidates, key=lambda lv: (self._jb_score(group, model, lv) - JB_COST * lv, -lv))

    def _after_success(self, strategy, modes, mode, used, probing, label):
        strategy.mode = mode
        base = self._base_level(modes)
        if probing:
            self._end_probe(strategy, refused=False)
            if strategy.probe_level >= 0 and used < strategy.level:
                LOGGER.info(
                    "[自动策略][%s] 试降成功，本文件之后改用 %s",
                    label, JB_LABELS[JB_LEVELS[used]],
                )
                strategy.level = max(used, base)
                strategy.probe_after = PROBE_AFTER
            strategy.probe_level = -1
            strategy.streak = 0
            return
        # 并发时，早于升级发出的低等级请求成功不算数；只统计当前等级上的连续成功
        if strategy.level <= base or used < strategy.level:
            if strategy.level <= base:
                strategy.streak = 0
            return
        strategy.streak += 1
        if strategy.streak < strategy.probe_after or strategy.probe_level >= 0:
            return
        lower = [lv for lv in self._levels(modes) if base <= lv < strategy.level]
        if not lower:
            return
        LOGGER.info(
            "[自动策略][%s] %s 已连续成功 %s 批，派下一批试着降回 %s 以节省开销",
            label, JB_LABELS[JB_LEVELS[strategy.level]], strategy.streak, JB_LABELS[JB_LEVELS[lower[-1]]],
        )
        strategy.probe_level = lower[-1]

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
        for attempt in range(MAX_RETRIES + 1):
            self._check_stop_requested()
            token_index = (
                self._preferred_tokens.get(file_key, 0) % num_tokens if num_tokens > 0 else 0
            )
            model = self._model_name(token_index)
            strategy = self._strategy(file_key, modes)
            level, probing = self._claim_level(strategy)
            mode, used = self._choose(modes, group, model, level, strategy.mode, excluded)
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
            # 失败模板不能把局部解析结果或失败标记写回正式译文。
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
                if count != len(current) or len(translated) != len(current):
                    raise TranslationParseError(f"译文缺句：{count}/{len(current)}")
                for original, result in zip(current, translated):
                    original.__dict__.update({key: value for key, value in result.__dict__.items() if key not in ("prev_tran", "next_tran")})
                # 会话连续性按正式句子对象判断，替换临时副本引用。
                for session in context["sessions"].values():
                    if session.last_tran is working[-1]:
                        session.last_tran = current[-1]
                self._active_token_index = token_index
                self._count("template", (group, model, mode), True)
                self._count("jailbreak", (group, model, jb), True)
                self._after_success(strategy, modes, mode, used, probing, label)
                probing = False
                return count, list(current)
            except (TranslationParseError, TranslationRequestError, APIError, httpx.HTTPError, OSError, ValueError) as exc:
                engine.reset_conversation(filename)
                kind = classify_failure(exc, jb)
                shrink = kind in ("refusal", "format")
                if kind == "format":
                    self._count("template", (group, model, mode), False)
                    excluded.add(mode)
                    following, _ = self._choose(modes, group, model, strategy.level, strategy.mode, set(excluded))
                    action = f"格式错误，切换 {following}" if following != mode else f"格式错误，保持 {mode}"
                elif kind == "refusal" and probing:
                    # 试降被拒说明还需要原来的等级，不是这批句子的问题：恢复原等级，不缩小批次
                    self._count("jailbreak", (group, model, jb), False)
                    self._end_probe(strategy, refused=True)
                    probing = False
                    shrink = False
                    action = f"试降后被拒，恢复{JB_LABELS[JB_LEVELS[strategy.level]]}"
                    LOGGER.info(
                        "[自动策略][%s] 试降后被拒，保持 %s，连续成功 %s 批后再试",
                        label, JB_LABELS[JB_LEVELS[strategy.level]], strategy.probe_after,
                    )
                elif kind == "refusal":
                    self._count("jailbreak", (group, model, jb), False)
                    stronger = self._stronger_level(modes, group, model, used)
                    strategy.streak = 0
                    if not strategy.probe_inflight:
                        strategy.probe_level = -1
                    if stronger is not None:
                        if stronger > strategy.level:
                            strategy.level = stronger
                            LOGGER.info(
                                "[自动策略][%s] 疑似拒答，本文件之后改用 %s",
                                label, JB_LABELS[JB_LEVELS[stronger]],
                            )
                        action = f"疑似拒答，改用{JB_LABELS[JB_LEVELS[strategy.level]]}"
                    else:
                        strategy.level = max(strategy.level, used)
                        action = "疑似拒答，已无更强的改善拒答方式，缩小批次隔离问题句"
                elif kind == "image_unsupported":
                    self._state()["image_disabled"] = True
                    self._strategy(file_key, modes)  # 把等级调整到仍可用的方式
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
            for (g, m, jb), (ok, bad) in sorted(state["jailbreak"].items()):
                if (g, m) == (group, model) and (jb != "none" or bad):
                    parts.append(f"{JB_LABELS[jb]}未被拒 {ok}/{ok + bad}")
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
