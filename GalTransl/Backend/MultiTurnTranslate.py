"""ForGal / ForNovel 共用的多轮翻译上下文；Sakura 保持自己的请求流程。"""

from contextvars import ContextVar

from GalTransl import LOGGER
from GalTransl.Backend.BaseTranslate import BaseTranslate, REASONING_FIELD_NAMES

# 改善拒答的两种方式：prefill 预置 assistant 回复开头；image 把前文和输入渲染成图片发送
ENHANCE_JAILBREAK_MODES = ("prefill", "image")
IMAGE_PLACEHOLDER = "(provided in the attached image)"

# 自动模式逐次指定本次请求的改善拒答方式（"" / "prefill" / "image"），优先于模板自己的配置。
# 用 ContextVar 而不是改引擎属性：同一个引擎会被并发的多个批次共用。
JAILBREAK_OVERRIDE: ContextVar = ContextVar("enhance_jailbreak_override", default=None)


class _TranslationSession:
    def __init__(self):
        self.reset()

    def reset(self):
        self.messages = []
        self.turns = 0
        self.chars = 0
        self.last_tran = None
        self.reasoning_field = ""

    def add_turn(self, prompt, reply, reasoning, last_tran):
        text = str(reasoning.get("text") or "")
        field = str(reasoning.get("field") or REASONING_FIELD_NAMES[0])
        if text:
            self.reasoning_field = field
        assistant = {"role": "assistant", "content": reply}
        if self.reasoning_field:
            assistant[self.reasoning_field] = text
        self.messages.extend([{"role": "user", "content": prompt}, assistant])
        self.turns += 1
        self.chars += len(prompt) + len(reply) + len(text)
        self.last_tran = last_tran


class MultiTurnTranslate(BaseTranslate):
    # 各模板保留自己的输入格式；首轮仍使用可自定义的完整 trans_prompt。
    followup_prompt = ""
    # 图片方式下附在提示词末尾，告诉模型读完图片后怎样交付结果
    image_mode_reply_hint = "reply in plain text in the required output format."

    def __init__(self, config, eng_type, proxy_pool=None, token_pool=None):
        super().__init__(config, eng_type, proxy_pool, token_pool)
        self.multi_turn = self._coerce_bool(config.getKey("gpt.multiTurn", True))
        self.multi_turn_max_turns = self._coerce_positive_int(
            config.getKey("gpt.multiTurn.maxTurns", 8), 8
        )
        self.multi_turn_max_chars = self._coerce_positive_int(
            config.getKey("gpt.multiTurn.maxChars", 24000), 24000
        )

    def _load_enhance_jailbreak(self, config):
        self.enhance_jailbreak = self._coerce_bool(config.getKey("gpt.enhance_jailbreak", False))
        mode = str(config.getKey("gpt.enhance_jailbreak.mode", "prefill") or "").strip().lower()
        self.enhance_jailbreak_mode = mode if mode in ENHANCE_JAILBREAK_MODES else "prefill"
        self.enhance_jailbreak_font = str(config.getKey("gpt.enhance_jailbreak.font", "") or "").strip()

    def _enhance_jailbreak_strategy(self, retry_toggle=False):
        """本次请求用哪种改善拒答方式；不需要时返回空串。

        常开（gpt.enhance_jailbreak）时每次都用；没开时解析失败的重试也会隔次启用。
        自动模式通过 JAILBREAK_OVERRIDE 直接指定，不看这两个条件。
        """
        override = JAILBREAK_OVERRIDE.get()
        if override is not None:
            return override
        if not (getattr(self, "enhance_jailbreak", False) or retry_toggle):
            return ""
        return getattr(self, "enhance_jailbreak_mode", "prefill")

    def _session_scope(self):
        # 延迟初始化兼容跳过 __init__ 的插件/测试替身。每个 batch 调用独立作用域，
        # 即使同时处理同名文件，也不会共享会话；递归拆批重试则留在同一作用域。
        if not hasattr(self, "_translation_session_scope"):
            self._translation_session_scope = ContextVar("translation_sessions", default=None)
            self._translation_sessions = {}
        return self._translation_session_scope

    def _sessions(self):
        scoped = self._session_scope().get()
        return scoped["sessions"] if scoped is not None else self._translation_sessions

    async def _batch_translate_common(self, **kwargs):
        scope = self._session_scope()
        token = scope.set({"sessions": {}, "context": {}})
        try:
            return await super()._batch_translate_common(**kwargs)
        finally:
            # 完成、取消和异常都释放该分块的历史，避免整本小说常驻内存。
            scope.reset(token)

    def restore_context(self, translist_unhit, num_pre_request, filename=""):
        if num_pre_request <= 0 or not getattr(self, "restore_context_mode", True):
            self.last_translations[filename] = ""
        else:
            super().restore_context(translist_unhit, num_pre_request, filename)
        scoped = self._session_scope().get()
        if scoped is not None:
            scoped["context"][filename] = self.last_translations[filename]

    def restore_source_context(self, translist_unhit, num_pre_request, filename=""):
        restored = super().restore_source_context(translist_unhit, num_pre_request, filename)
        scoped = self._session_scope().get()
        if restored and scoped is not None:
            scoped["context"][filename] = self.last_translations[filename]
        return restored

    def _apply_history_result(self, prompt_req, filename):
        scoped = self._session_scope().get()
        if scoped is None:
            return super()._apply_history_result(prompt_req, filename)
        history = scoped["context"].get(filename, "")
        return prompt_req.replace("[history_result]", history.replace("<br>", "") if history else "None")

    def reset_conversation(self, filename=""):
        self.last_translations[filename] = ""
        scoped = self._session_scope().get()
        if scoped is not None:
            scoped["context"][filename] = ""
        for key, session in self._sessions().items():
            if key[0] == filename:
                session.reset()

    def _prepare_translation_messages(
        self, prompt_template, input_src, gptdict, trans_list, filename,
        proofread=False, assistant_prompt="", image_mode=False,
    ):
        label = f"{filename}:{self._build_idx_tip(trans_list)}" if trans_list else filename
        if image_mode:
            prepared = self._prepare_image_translation_messages(input_src, gptdict, filename, label)
            if prepared is not None:
                return prepared
        if assistant_prompt:
            self._log_enhance_jailbreak(label, "预置回复开头")
        session = None
        prompt_req = self._apply_history_result(prompt_template, filename)
        messages = [{"role": "system", "content": self.system_prompt}]
        if getattr(self, "multi_turn", True):
            session = self._sessions().setdefault((filename, proofread), _TranslationSession())
            # 缓存命中造成的跳段、重翻、切换文件内容，都应重新带入对应位置的前文。
            if session.messages and trans_list[0].prev_tran is not session.last_tran:
                session.reset()
            followup = self.followup_prompt.replace("[Input]", input_src).replace("[Glossary]", gptdict)
            request_chars = len(self.system_prompt) + len(followup) + len(assistant_prompt)
            if (
                session.turns >= getattr(self, "multi_turn_max_turns", 8)
                or session.chars + request_chars > getattr(self, "multi_turn_max_chars", 24000)
            ):
                session.reset()
            if session.messages:
                prompt_req = followup
                # 复制消息，补齐 thinking 接口要求的空字段，不修改此前发出的请求。
                for old_message in session.messages:
                    message = dict(old_message)
                    if message["role"] == "assistant" and session.reasoning_field:
                        message.setdefault(session.reasoning_field, "")
                    messages.append(message)
        messages.append({"role": "user", "content": prompt_req})
        if assistant_prompt:
            prefill = {"role": "assistant", "content": assistant_prompt}
            if session is not None and session.reasoning_field:
                prefill[session.reasoning_field] = ""
            messages.append(prefill)
        return messages, session, prompt_req

    def _log_enhance_jailbreak(self, label, method):
        """改善拒答真正作用到这次请求时打一条日志，并注明是常开还是解析失败后的临时启用。"""
        if JAILBREAK_OVERRIDE.get() is not None:
            reason = "自动模式选择"
        elif getattr(self, "enhance_jailbreak", False):
            reason = "已开启改善拒答"
        else:
            reason = "解析失败重试，临时启用"
        LOGGER.info(f"[改善拒答][{label}] 已生效（{reason}）：{method}")

    def _prepare_image_translation_messages(self, input_src, gptdict, filename, label=""):
        """图片方式：前文（history_result）和本批输入渲染成图片，其余提示词照常以文字发送。

        这次请求不带多轮历史（历史里就是文字形式的前文），用完整的首轮提示词单独发送；
        返回的 session 为 None，结果不记入多轮会话，下一批会按前文重新开始。
        渲染失败返回 None，调用方退回普通文字请求。
        """
        history = self._apply_history_result("[history_result]", filename)
        has_history = history.strip() not in ("", "None")
        sections = []
        if has_history:
            sections.append(f"<history_result>\n{history}\n</history_result>")
        sections.append(f"<input>\n{input_src}\n</input>")
        try:
            from GalTransl.Backend.TextImage import render_text_data_urls

            image_urls = render_text_data_urls(
                "\n\n".join(sections), getattr(self, "enhance_jailbreak_font", "")
            )
        except Exception as exc:
            LOGGER.warning(f"[改善拒答][{label or filename}] 渲染图片失败，本次改用文字发送：{exc}")
            return None

        prompt_text = self._build_prompt_request(IMAGE_PLACEHOLDER, gptdict)
        prompt_text = prompt_text.replace(
            "[history_result]", IMAGE_PLACEHOLDER if has_history else "None"
        )
        parts = "the <history_result> and <input> contents are" if has_history else "the <input> content is"
        prompt_text = (
            prompt_text.rstrip()
            + f"\n\nNote: {parts} provided as {len(image_urls)} attached image(s), in reading order. "
            "Read them carefully, copy every hash anchor, ID, tag and symbol exactly as shown, "
            f"and {self.image_mode_reply_hint}"
        )
        content = [{"type": "text", "text": prompt_text}] + [
            {"type": "image_url", "image_url": {"url": url, "detail": "high"}}
            for url in image_urls
        ]
        self._log_enhance_jailbreak(
            label or filename, f"前文与输入以图片发送（{len(image_urls)} 张）"
        )
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": content},
        ]
        return messages, None, prompt_text

    async def _ask_translation_chatbot(self, session, **kwargs):
        try:
            return await self.ask_chatbot(**kwargs)
        except BaseException:
            if session is not None:
                session.reset()
            raise

    @staticmethod
    def _remember_translation_turn(
        session, prompt, reply, reasoning, trans_list, success_count,
        error_message, assistant_prompt="",
    ):
        if session is None:
            return
        # 部分解析虽能推进进度，却不能把缺句或错误输出当成后续的格式示例。
        if success_count != len(trans_list) or error_message:
            session.reset()
            return
        if assistant_prompt and not reply.lstrip().startswith("```"):
            reply = assistant_prompt + "\n" + reply
        session.add_turn(prompt, reply, reasoning, trans_list[-1])
