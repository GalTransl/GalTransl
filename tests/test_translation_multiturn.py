"""多轮翻译经过实际模板输入/输出解析，覆盖流式、重试、断点与并发隔离。"""

import asyncio
import copy
import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from GalTransl.Backend.BaseTranslate import BaseTranslate, _CHATBOT_STATE
from GalTransl.Backend.ForGalJsonTranslate import ForGalJsonTranslate
from GalTransl.Backend.ForGalTsvTranslate import ForGalTsvTranslate
from GalTransl.Backend.ForNovelTranslate import ForNovelTranslate
from GalTransl.Backend.Prompts import (
    FORGAL_JSON_TRANS_PROMPT, FORGAL_TSV_TRANS_PROMPT_EN, FORNOVEL_TRANS_PROMPT_EN,
)
from GalTransl.CSentense import CSentense


ENGINES = (
    (ForGalJsonTranslate, FORGAL_JSON_TRANS_PROMPT),
    (ForGalTsvTranslate, FORGAL_TSV_TRANS_PROMPT_EN),
    (ForNovelTranslate, FORNOVEL_TRANS_PROMPT_EN),
)


def sentences(count, prefix="source"):
    items = [CSentense(f"{prefix}-{i}", index=i) for i in range(1, count + 1)]
    for prev, current in zip(items, items[1:]):
        prev.next_tran, current.prev_tran = current, prev
    return items


def translator(kind, prompt):
    engine = kind.__new__(kind)
    engine.pj_config = SimpleNamespace(
        active_workers=0, translation_guideline="CUSTOM-GUIDELINE",
        stop_event=None, getProjectDir=lambda: "", bar=lambda *args: None,
    )
    engine.system_prompt = "system"
    engine.trans_prompt = prompt
    engine.contextNum = 0
    engine.last_translations = {}
    engine.enhance_jailbreak = False
    engine.target_lang = "English"
    engine.source_lang = "Japanese"
    engine.smartRetry = False
    engine.max_api_retries = 1
    engine.skipH = False
    engine.save_steps = 999
    engine.dynamic_num_per_request = False
    engine._interruptible_sleep = AsyncMock()
    engine._record_runtime_success = lambda *args: None
    return engine


class Model:
    def __init__(self, engine, *, stream=False, actions=(), reasoning_field="reasoning_content"):
        self.engine = engine
        self.stream = stream
        self.actions = list(actions)
        self.reasoning_field = reasoning_field
        self.requests = []
        self.replies = []

    async def __call__(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs["messages"]))
        prompt = next(m["content"] for m in reversed(kwargs["messages"]) if m["role"] == "user")
        payload = prompt.split("<input>")[-1].split("</input>")[0]
        rows = []
        for line in payload.splitlines():
            if isinstance(self.engine, ForGalJsonTranslate):
                match = re.match(r'([a-z0-9]{3})\|(\{.*\})', line)
                if not match:
                    continue
                sig, obj = match.groups()
                obj = json.loads(obj)
                rows.append(sig + "|" + json.dumps({"id": obj["id"], "dst": "translated-" + obj["src"]}))
            elif line and line.rsplit("\t", 1)[-1].isdigit():
                src = line.split("\t")[-2]
                index = line.rsplit("\t", 1)[-1]
                prefix = "null\t" if isinstance(self.engine, ForGalTsvTranslate) else ""
                rows.append(prefix + "translated-" + src + "\t" + index)
        action = self.actions.pop(0) if self.actions else None
        if isinstance(action, BaseException):
            raise action
        if action == "bad":
            rows = ["invalid response"]
        elif action == "partial":
            rows = rows[:1]
        reasoning = "" if action == "no-reasoning" else f"thinking-{len(self.requests)}"
        kwargs["reasoning_holder"].update(field=self.reasoning_field, text=reasoning)
        await asyncio.sleep(0)  # 让并发请求实际交错
        _CHATBOT_STATE.set((self.stream, "test-model"))
        if self.stream:
            kwargs["stream_line_callback"](rows, True)
        reply = "\n".join(rows)
        self.replies.append(reply)
        return reply, SimpleNamespace(model_name="test-model")


class TranslationMultiTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_followups_keep_history_reasoning_and_current_glossary(self):
        for kind, prompt in ENGINES:
            for stream in (False, True):
                for field in ("reasoning_content", "reasoning"):
                    with self.subTest(engine=kind.__name__, stream=stream, field=field):
                        engine = translator(kind, prompt)
                        model = Model(engine, stream=stream, reasoning_field=field)
                        engine.ask_chatbot = model
                        items = sentences(3)
                        for i, item in enumerate(items):
                            count, result = await engine.translate([item], f"glossary-{i}", filename="part")
                            self.assertEqual(count, 1)
                            self.assertEqual(result[0].pre_dst, f"translated-source-{i + 1}")
                        first, second, third = model.requests
                        self.assertEqual([m["role"] for m in second], ["system", "user", "assistant", "user"])
                        self.assertEqual(first, second[:2])
                        self.assertEqual(second, third[:4])
                        self.assertIn("CUSTOM-GUIDELINE", first[1]["content"])
                        self.assertNotIn("CUSTOM-GUIDELINE", second[-1]["content"])
                        self.assertIn("glossary-1", second[-1]["content"])
                        self.assertNotIn("glossary-0", second[-1]["content"])
                        self.assertEqual(second[2]["content"], model.replies[0])
                        self.assertEqual(second[2][field], "thinking-1")
                        self.assertEqual(third[4][field], "thinking-2")
                        self.assertNotIn("[Input]", second[-1]["content"])

    async def test_turn_limit_restarts_with_recent_translations(self):
        for kind, prompt in ENGINES:
            with self.subTest(engine=kind.__name__):
                engine = translator(kind, prompt)
                engine.multi_turn_max_turns = 2
                engine.contextNum = 1
                model = Model(engine)
                engine.ask_chatbot = model
                for item in sentences(3):
                    await engine.translate([item], filename="part")
                self.assertEqual([len(r) for r in model.requests], [2, 4, 2])
                restarted = model.requests[-1][-1]["content"]
                self.assertIn("CUSTOM-GUIDELINE", restarted)
                self.assertIn("translated-source-2", restarted)
                self.assertNotIn("translated-source-1", restarted)

    async def test_character_budget_counts_reasoning_and_next_request(self):
        for kind, prompt in ENGINES:
            with self.subTest(engine=kind.__name__):
                engine = translator(kind, prompt)
                model = Model(engine)
                engine.ask_chatbot = model
                items = sentences(3)
                await engine.translate(items[:1], filename="part")
                session = engine._sessions()[("part", False)]
                self.assertEqual(session.chars, sum(len(m["content"]) for m in session.messages) + len("thinking-1"))
                engine.multi_turn_max_chars = session.chars + 1
                await engine.translate(items[1:2], filename="part")
                self.assertEqual(len(model.requests[-1]), 2)
                # 单批超过预算也完整发送，不截断输入。
                engine.multi_turn_max_chars = 1
                await engine.translate(items[2:], filename="part")
                self.assertIn("source-3", model.requests[-1][-1]["content"])

    async def test_failed_attempt_is_not_added_and_partial_result_resets(self):
        for kind, prompt in ENGINES:
            with self.subTest(engine=kind.__name__):
                engine = translator(kind, prompt)
                model = Model(engine, actions=[None, "bad", None, "partial", None])
                engine.ask_chatbot = model
                items = sentences(5)
                await engine.translate(items[:1], filename="part")
                await engine.translate(items[1:2], filename="part")
                self.assertNotIn("invalid response", str(model.requests[2]))
                self.assertEqual(len(engine._sessions()[("part", False)].messages), 4)
                count, _ = await engine.translate(items[2:4], filename="part")
                self.assertEqual(count, 1)
                await engine.translate(items[3:4], filename="part")
                self.assertEqual(len(model.requests[-1]), 2)

    async def test_exhausted_parse_retries_clear_history(self):
        for kind, prompt in ENGINES:
            with self.subTest(engine=kind.__name__):
                engine = translator(kind, prompt)
                model = Model(engine, actions=[None, "bad", "bad", "bad", "bad"])
                engine.ask_chatbot = model
                items = sentences(3)
                await engine.translate(items[:1], filename="part")
                await engine.translate(items[1:2], filename="part")
                self.assertEqual(engine._sessions()[("part", False)].messages, [])
                await engine.translate(items[2:], filename="part")
                self.assertEqual(len(model.requests[-1]), 2)

    async def test_smart_retry_split_keeps_only_accepted_turn(self):
        for kind, prompt in ENGINES:
            with self.subTest(engine=kind.__name__):
                engine = translator(kind, prompt)
                engine.smartRetry = True
                model = Model(engine, actions=[None, "bad", "bad"])
                engine.ask_chatbot = model
                items = sentences(5)
                await engine.translate(items[:1], filename="part")
                count, _ = await engine.translate(items[1:4], filename="part")
                self.assertEqual(count, 1)
                await engine.translate(items[2:3], filename="part")
                self.assertEqual(len(model.requests[-1]), 6)
                self.assertNotIn("invalid response", str(model.requests[-1]))
                self.assertNotIn("source-3", model.requests[-1][-3]["content"])

    async def test_api_failure_and_cancellation_clear_history(self):
        for kind, prompt in ENGINES:
            for error in (RuntimeError("API failed"), asyncio.CancelledError()):
                with self.subTest(engine=kind.__name__, error=type(error).__name__):
                    engine = translator(kind, prompt)
                    model = Model(engine, actions=[None, error])
                    engine.ask_chatbot = model
                    items = sentences(2)
                    await engine.translate(items[:1], filename="part")
                    with self.assertRaises(type(error)):
                        await engine.translate(items[1:], filename="part")
                    self.assertEqual(engine._sessions()[("part", False)].messages, [])

    async def test_cached_gaps_and_file_changes_start_new_session(self):
        for kind, prompt in ENGINES:
            with self.subTest(engine=kind.__name__):
                engine = translator(kind, prompt)
                engine.contextNum = 1
                model = Model(engine)
                engine.ask_chatbot = model
                items = sentences(4)
                items[1].pre_dst = "cached translation"
                await engine.translate(items[:1], filename="part")
                await engine.translate(items[2:3], filename="part")
                self.assertEqual(len(model.requests[-1]), 2)
                self.assertIn("cached translation", model.requests[-1][-1]["content"])
                await engine.translate(items[3:], filename="other")
                self.assertEqual(len(model.requests[-1]), 2)

    async def test_switch_off_uses_single_turn_and_zero_context_has_no_seed(self):
        for kind, prompt in ENGINES:
            with self.subTest(engine=kind.__name__):
                engine = translator(kind, prompt)
                engine.multi_turn = False
                engine.contextNum = 1
                model = Model(engine)
                engine.ask_chatbot = model
                items = sentences(3)
                for item in items[:2]:
                    await engine.translate([item], filename="part")
                self.assertEqual([len(r) for r in model.requests], [2, 2])
                self.assertIn("translated-source-1", model.requests[-1][-1]["content"])
                engine.contextNum = 0
                await engine.translate(items[2:], filename="part")
                self.assertNotIn("translated-source", model.requests[-1][-1]["content"])

    async def test_empty_reasoning_and_prefill_are_valid_assistant_history(self):
        engine = translator(*ENGINES[2])
        engine.enhance_jailbreak = True
        model = Model(engine, actions=["no-reasoning", None, "no-reasoning", None])
        engine.ask_chatbot = model
        for item in sentences(4):
            await engine.translate([item], filename="part")
        third, fourth = model.requests[2:]
        self.assertEqual(third[2]["reasoning_content"], "")
        self.assertEqual(third[-1]["reasoning_content"], "")
        self.assertTrue(third[2]["content"].startswith("```DST\tID"))
        self.assertEqual(fourth[6]["reasoning_content"], "")
        self.assertEqual([m["role"] for m in fourth], ["system", "user", "assistant", "user", "assistant", "user", "assistant", "user", "assistant"])

    async def test_concurrent_batches_with_same_filename_are_isolated_and_released(self):
        for kind, prompt in ENGINES:
            with self.subTest(engine=kind.__name__):
                engine = translator(kind, prompt)
                model = Model(engine)
                engine.ask_chatbot = model

                async def run(prefix):
                    items = sentences(3, prefix)
                    return await engine.batch_translate(
                        "same-file", "unused-cache", items, 1, translist_unhit=items,
                    )

                a, b = await asyncio.gather(run("alpha"), run("beta"))
                self.assertEqual(len(a), 3)
                self.assertEqual(len(b), 3)
                self.assertEqual(sorted(map(len, model.requests)), [2, 2, 4, 4, 6, 6])
                for request in model.requests:
                    text = str(request)
                    self.assertNotEqual("alpha" in text, "beta" in text)
                self.assertEqual(engine._sessions(), {})
                self.assertIsNone(engine._session_scope().get())

    async def test_batch_cancellation_releases_scope(self):
        engine = translator(*ENGINES[2])
        engine.ask_chatbot = Model(engine, actions=[None, asyncio.CancelledError()])
        items = sentences(3)
        with self.assertRaises(asyncio.CancelledError):
            await engine.batch_translate("part", "unused", items, 1, translist_unhit=items)
        self.assertIsNone(engine._session_scope().get())
        self.assertEqual(engine._sessions(), {})

    async def test_concurrent_retry_keeps_each_batches_cached_context(self):
        for kind, prompt in ENGINES:
            with self.subTest(engine=kind.__name__):
                engine = translator(kind, prompt)
                engine.contextNum = 1
                model = Model(engine, actions=["bad", "bad"])
                engine.ask_chatbot = model

                async def run(prefix):
                    items = sentences(3, prefix)
                    items[0].pre_dst = "cached-" + prefix
                    return await engine.batch_translate(
                        "same-file", "unused", items, 1, translist_unhit=items[1:],
                    )

                await asyncio.gather(run("alpha"), run("beta"))
                self.assertEqual(len(model.requests), 6)
                for request in model.requests:
                    text = str(request)
                    self.assertNotEqual("alpha" in text, "beta" in text)

    async def test_explicit_reset_and_disabled_restore_do_not_reseed_history(self):
        for kind, prompt in ENGINES:
            with self.subTest(engine=kind.__name__):
                engine = translator(kind, prompt)
                engine.contextNum = 1
                engine.restore_context_mode = False
                model = Model(engine)
                engine.ask_chatbot = model
                items = sentences(2)
                await engine.translate(items[:1], filename="part")
                engine.reset_conversation("part")
                await engine.translate(items[1:], filename="part")
                self.assertEqual(len(model.requests[-1]), 2)
                self.assertNotIn("translated-source-1", model.requests[-1][-1]["content"])

    def test_configuration_defaults_and_explicit_disable(self):
        for settings in ({}, {"gpt.multiTurn": "false", "gpt.multiTurn.maxTurns": 3, "gpt.multiTurn.maxChars": 8000}):
            for kind, _ in ENGINES:
                with self.subTest(engine=kind.__name__, settings=settings):
                    config = SimpleNamespace(getKey=lambda key, default=None: settings.get(key, default))
                    with (
                        patch.object(BaseTranslate, "__init__", return_value=None),
                        patch.object(kind, "init_chatbot"),
                        patch.object(kind, "_set_temp_type"),
                        patch.object(kind, "_apply_internal_prompt_template_overrides"),
                    ):
                        engine = kind(config, "", None, None)
                    self.assertEqual(engine.multi_turn, not bool(settings))
                    self.assertEqual(engine.multi_turn_max_turns, 3 if settings else 8)
                    self.assertEqual(engine.multi_turn_max_chars, 8000 if settings else 24000)


if __name__ == "__main__":
    unittest.main()
