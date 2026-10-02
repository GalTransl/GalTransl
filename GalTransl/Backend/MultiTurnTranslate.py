"""ForGal / ForNovel 共用的多轮翻译上下文；Sakura 保持自己的请求流程。"""

from contextvars import ContextVar

from GalTransl.Backend.BaseTranslate import BaseTranslate, REASONING_FIELD_NAMES


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

    def __init__(self, config, eng_type, proxy_pool=None, token_pool=None):
        super().__init__(config, eng_type, proxy_pool, token_pool)
        self.multi_turn = self._coerce_bool(config.getKey("gpt.multiTurn", True))
        self.multi_turn_max_turns = self._coerce_positive_int(
            config.getKey("gpt.multiTurn.maxTurns", 8), 8
        )
        self.multi_turn_max_chars = self._coerce_positive_int(
            config.getKey("gpt.multiTurn.maxChars", 24000), 24000
        )

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
        proofread=False, assistant_prompt="",
    ):
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
