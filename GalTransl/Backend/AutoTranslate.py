"""按文件字段选择模板，首次错误立即切换格式并拆分重试。"""

from contextvars import ContextVar
from copy import copy

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
from GalTransl.Backend.MultiTurnTranslate import MultiTurnTranslate


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


class AutoTranslate(BaseTranslate):
    def __init__(self, config, eng_type, proxy_pool, token_pool):
        super().__init__(config, eng_type, proxy_pool, token_pool)
        self._proxy_pool, self._token_pool = proxy_pool, token_pool
        self._engines = {}
        self._preferred_modes = {}
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

    async def translate(self, trans_list, gptdict="", proofread=False, filename=""):
        if not trans_list:
            return 0, []
        has_name = any(getattr(tran, "source_file_has_name", bool(tran.speaker)) for tran in trans_list)
        modes = GAL_MODES if has_name else NOVEL_MODES
        if getattr(self, "_special_mode", None):
            modes = (self._special_mode,)
        file_key = (getattr(trans_list[0], "source_file_path", filename), has_name)
        state = self._batch_scope.get() or {"contexts": {}, "gpt_dic": None}
        current = trans_list
        for attempt in range(MAX_RETRIES + 1):
            self._check_stop_requested()
            index = self._preferred_modes.get(file_key, 0)
            mode = modes[index]
            engine = self._get_engine(mode)
            LOGGER.info("[auto-translate][%s:%s] %s，尝试 %s/%s", filename, self._build_idx_tip(current), mode, attempt + 1, MAX_RETRIES + 1)
            context = state["contexts"].setdefault(mode, {"sessions": {}, "context": {}})
            session_scope = engine._session_scope()
            session_token = session_scope.set(context)
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
                return count, list(current)
            except (TranslationParseError, TranslationRequestError, APIError, httpx.HTTPError, OSError, ValueError) as exc:
                engine.reset_conversation(filename)
                # 同文件的并行切片可能已经切换；成功/失败的旧请求不能回滚新选择。
                if self._preferred_modes.get(file_key, 0) == index:
                    self._preferred_modes[file_key] = (index + 1) % len(modes)
                next_mode = modes[self._preferred_modes[file_key]]
                action = f"保持 {mode}" if len(modes) == 1 else f"切换 {next_mode}"
                retry_size = max(1, len(current) // 2)
                exhausted = attempt == MAX_RETRIES
                detail = (
                    "共 5 次请求（4 次重试）仍失败，标记翻译失败"
                    if exhausted else f"{action}，缩小为 {retry_size} 句重试（{attempt + 1}/4）"
                )
                try:
                    from GalTransl.server import record_runtime_error

                    record_runtime_error(
                        getattr(self.pj_config, "runtime_project_dir", None) or self.pj_config.getProjectDir(),
                        kind="parse" if isinstance(exc, (TranslationParseError, ValueError)) else "api",
                        message=f"[auto-translate][{mode}] {type(exc).__name__}: {exc}；{detail}",
                        filename=filename,
                        index_range=self._build_idx_tip(current),
                        retry_count=attempt,
                        model=engine._get_chatbot_state()[1],
                        sleep_seconds=0.0 if exhausted else 1.0,
                        level="error" if exhausted else "warning",
                    )
                except Exception:
                    LOGGER.debug("自动翻译错误写入运行时记录失败", exc_info=True)
                if attempt == MAX_RETRIES:
                    LOGGER.error("[auto-translate][%s:%s] 共 5 次请求（4 次重试）仍失败：%s", filename, self._build_idx_tip(current), exc)
                    failed = []
                    self._append_parse_failure_fallback_results(
                        current, 0, failed, mode, proofread=proofread,
                        translate_failed_prefix="(Failed)", translate_problem_message="翻译失败",
                    )
                    return len(failed), failed
                current = current[:retry_size]
                LOGGER.warning("[auto-translate][%s] %s 出错：%s；%s，缩小为 %s 句重试（%s/4）", filename, mode, exc, action, len(current), attempt + 1)
                await self._interruptible_sleep(1)
            finally:
                session_scope.reset(session_token)

    async def shutdown(self):
        try:
            for engine in self._engines.values():
                await engine.shutdown()
        finally:
            self._engines.clear()
            self._preferred_modes.clear()
            await super().shutdown()
