"""改善拒答的两种方式：prefill 预置回复开头；image 把前文和输入渲染成图片发送。"""

import base64
import copy
import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from GalTransl import LOGGER
from GalTransl.Backend import TextImage
from GalTransl.Backend.BaseTranslate import BaseTranslate
from GalTransl.Backend.ForGalToolTranslate import ForGalToolTranslate
from GalTransl.Backend.ForNovelToolTranslate import ForNovelToolTranslate
from GalTransl.Backend.MultiTurnTranslate import IMAGE_PLACEHOLDER
from GalTransl.Backend.Prompts import FORGAL_TOOL_TRANS_PROMPT, FORNOVEL_TOOL_TRANS_PROMPT
from test_forgal_tool import function_call, tool_input_rows
from test_translation_multiturn import ENGINES, Model, sentences, translator

TOOL_ENGINES = (
    (ForGalToolTranslate, FORGAL_TOOL_TRANS_PROMPT),
    (ForNovelToolTranslate, FORNOVEL_TOOL_TRANS_PROMPT),
)


class ImageModel(Model):
    """图片请求读不了文字：用渲染时记下的文本代替图片内容，交给原来的假模型解析。"""

    def __init__(self, engine, rendered, **kwargs):
        super().__init__(engine, **kwargs)
        self.rendered = rendered

    async def __call__(self, **kwargs):
        messages = kwargs["messages"]
        user = messages[-1]
        if isinstance(user["content"], list):
            text = user["content"][0]["text"] + "\n" + self.rendered[-1]
            kwargs = dict(kwargs, messages=messages[:-1] + [{"role": "user", "content": text}])
            reply = await super().__call__(**kwargs)
            self.requests[-1] = messages  # 记录真实发出的请求
            return reply
        return await super().__call__(**kwargs)


def fake_render(rendered):
    def render(text, font_path=""):
        rendered.append(text)
        return ["data:image/png;base64,AAAA"]
    return render


class EnhanceJailbreakImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_mode_renders_history_and_input_instead_of_text(self):
        for kind, prompt in ENGINES:
            with self.subTest(engine=kind.__name__):
                engine = translator(kind, prompt)
                engine.enhance_jailbreak = True
                engine.enhance_jailbreak_mode = "image"
                engine.contextNum = 1
                rendered = []
                model = ImageModel(engine, rendered)
                engine.ask_chatbot = model
                items = sentences(2)
                with patch.object(TextImage, "render_text_data_urls", fake_render(rendered)):
                    for item in items:
                        count, result = await engine.translate([item], "glossary", filename="part")
                        self.assertEqual(count, 1)
                        self.assertEqual(result[0].pre_dst, "translated-" + item.post_src)

                second = model.requests[-1]
                self.assertEqual([m["role"] for m in second], ["system", "user"])
                text_part, image_part = second[1]["content"]
                self.assertEqual(image_part["type"], "image_url")
                self.assertTrue(image_part["image_url"]["url"].startswith("data:image/png;base64,"))
                # 前文和输入只在图片里，提示词的其余部分（规范、词表）照常是文字
                self.assertNotIn("source-2", text_part["text"])
                self.assertNotIn("translated-source-1", text_part["text"])
                self.assertIn(IMAGE_PLACEHOLDER, text_part["text"])
                self.assertIn("CUSTOM-GUIDELINE", text_part["text"])
                self.assertIn("glossary", text_part["text"])
                self.assertIn("<history_result>", rendered[-1])
                self.assertIn("translated-source-1", rendered[-1])
                self.assertIn("source-2", rendered[-1].split("<input>")[1])

    async def test_parse_failure_retry_switches_to_image_when_mode_is_image(self):
        engine = translator(*ENGINES[0])
        engine.enhance_jailbreak_mode = "image"
        rendered = []
        model = ImageModel(engine, rendered, actions=["bad", None])
        engine.ask_chatbot = model
        with patch.object(TextImage, "render_text_data_urls", fake_render(rendered)):
            count, _ = await engine.translate(sentences(1), filename="part")
        self.assertEqual(count, 1)
        first, second = model.requests
        self.assertIsInstance(first[-1]["content"], str)
        self.assertIsInstance(second[-1]["content"], list)
        self.assertNotEqual(second[-1]["role"], "assistant")

    async def test_prefill_mode_is_unchanged(self):
        engine = translator(*ENGINES[0])
        engine.enhance_jailbreak = True
        model = Model(engine)
        engine.ask_chatbot = model
        await engine.translate(sentences(1), filename="part")
        self.assertEqual(model.requests[0][-1], {"role": "assistant", "content": "```jsonline"})

    async def test_logs_when_enhancement_takes_effect(self):
        def messages(logs):
            return [line for line in logs.output if "[改善拒答]" in line]

        # 常开 + 预置补全：每次请求都提示
        engine = translator(*ENGINES[0])
        engine.enhance_jailbreak = True
        engine.ask_chatbot = Model(engine)
        with self.assertLogs("GalTransl", "INFO") as logs:
            await engine.translate(sentences(2), filename="part")
        self.assertEqual(len(messages(logs)), 1)
        self.assertIn("[改善拒答][part:1~2] 已生效（已开启改善拒答）：预置回复开头", messages(logs)[0])

        # 未开启：第一次请求不提示，解析失败后的重试临时启用图片方式时提示
        engine = translator(*ENGINES[0])
        engine.enhance_jailbreak_mode = "image"
        rendered = []
        engine.ask_chatbot = ImageModel(engine, rendered, actions=["bad", None])
        with patch.object(TextImage, "render_text_data_urls", fake_render(rendered)):
            with self.assertLogs("GalTransl", "INFO") as logs:
                await engine.translate(sentences(1), filename="part")
        self.assertEqual(len(messages(logs)), 1)
        self.assertIn("已生效（解析失败重试，临时启用）：前文与输入以图片发送（1 张）", messages(logs)[0])

        # 预置补全对 Markdown / tool 不改变请求，不应打出「已生效」
        engine = translator(*ENGINES[1])
        engine.enhance_jailbreak = True
        engine.ask_chatbot = Model(engine)
        with self.assertLogs("GalTransl", "INFO") as logs:
            LOGGER.info("sentinel")  # assertLogs 要求至少一条日志
            await engine.translate(sentences(1), filename="part")
        self.assertEqual(messages(logs), [])

    def test_server_job_terminal_filter_lets_enhancement_log_through(self):
        import logging
        from GalTransl.Runner import _ServerStatusFilter

        def record(message):
            return logging.LogRecord("GalTransl", logging.INFO, __file__, 1, message, None, None)

        status_filter = _ServerStatusFilter()
        self.assertTrue(status_filter.filter(record("[改善拒答][a.json:1~16] 已生效（已开启改善拒答）：前文与输入以图片发送（1 张）")))
        self.assertFalse(status_filter.filter(record("->翻译输入：...")))

    async def test_render_failure_falls_back_to_text_request(self):
        engine = translator(*ENGINES[2])
        engine.enhance_jailbreak = True
        engine.enhance_jailbreak_mode = "image"
        model = Model(engine)
        engine.ask_chatbot = model

        def broken(*args, **kwargs):
            raise OSError("no font")

        with patch.object(TextImage, "render_text_data_urls", broken):
            count, _ = await engine.translate(sentences(1), filename="part")
        self.assertEqual(count, 1)
        self.assertIsInstance(model.requests[0][-1]["content"], str)

    async def test_tool_templates_send_images_and_still_write_with_tool(self):
        for kind, prompt in TOOL_ENGINES:
            with self.subTest(engine=kind.__name__):
                engine = translator(kind, prompt)
                engine.multi_turn = False
                engine.enhance_jailbreak = True
                engine.enhance_jailbreak_mode = "image"
                engine.contextNum = 1
                rendered, requests = [], []

                async def model(**kwargs):
                    messages = kwargs["messages"]
                    requests.append(copy.deepcopy(messages))
                    content = messages[1]["content"]
                    self.assertIsInstance(content, list)  # 每轮工具往返都带着图片
                    rows = tool_input_rows(rendered[-1])
                    body = "\n".join(f"@@ {sig}|{obj['id']}\n+translated-{obj['src']}" for sig, obj in rows)
                    calls = [function_call("write_translation_result", f"*** Begin Patch\n{body}\n*** End Patch")]
                    kwargs["tool_response_holder"].update(tool_calls=calls, finish_reason="tool_calls")
                    return "", SimpleNamespace(model_name="test")

                engine.ask_chatbot = model
                items = sentences(3)
                items[0].pre_dst = "cached-first"
                with patch.object(TextImage, "render_text_data_urls", fake_render(rendered)), redirect_stdout(io.StringIO()):
                    result = await engine.batch_translate("file", "unused", items, 2, translist_unhit=items[1:])
                self.assertEqual([item.pre_dst for item in result], ["translated-source-2", "translated-source-3"])
                text_part = requests[0][1]["content"][0]["text"]
                self.assertNotIn("source-2", text_part)
                self.assertNotIn("cached-first", text_part)
                self.assertIn("write_translation_result as usual", text_part)
                self.assertIn("cached-first", rendered[0].split("<input>")[0])
                self.assertNotIn("assistant", [m["role"] for m in requests[0]])

    async def test_tool_refusal_without_tool_call_switches_to_image_instead_of_nudging(self):
        refusal = "The prompt could not be submitted. The prompt contains sensitive words."
        for mode, expected_kinds in (("image", [str, list]), ("prefill", [str, str, str])):
            with self.subTest(mode=mode):
                engine = translator(*TOOL_ENGINES[0])
                engine.multi_turn = False
                engine.enhance_jailbreak_mode = mode
                engine.smartRetry = False
                rendered, requests = [], []

                async def model(**kwargs):
                    messages = kwargs["messages"]
                    requests.append(copy.deepcopy(messages))
                    if isinstance(messages[1]["content"], str):
                        kwargs["tool_response_holder"].update(tool_calls=[], finish_reason="stop")
                        return refusal, SimpleNamespace(model_name="test")
                    rows = tool_input_rows(rendered[-1])
                    body = "\n".join(f"@@ {sig}|{obj['id']}\n+translated-{obj['src']}" for sig, obj in rows)
                    kwargs["tool_response_holder"].update(
                        tool_calls=[function_call("write_translation_result", f"*** Begin Patch\n{body}\n*** End Patch")],
                        finish_reason="tool_calls",
                    )
                    return "", SimpleNamespace(model_name="test")

                engine.ask_chatbot = model
                with patch.object(TextImage, "render_text_data_urls", fake_render(rendered)), \
                        redirect_stdout(io.StringIO()), self.assertLogs("GalTransl", "INFO") as logs:
                    count, result = await engine.translate(sentences(1), filename="part")
                # 原文是第一轮的文字请求；image 方式下第二次请求就换成图片，不再原样追问
                self.assertEqual([type(r[1]["content"]) for r in requests[:len(expected_kinds)]], expected_kinds)
                if mode == "image":
                    self.assertEqual(count, 1)
                    self.assertEqual(result[0].pre_dst, "translated-source-1")
                    self.assertTrue(any("疑似拒答" in line for line in logs.output))
                    self.assertTrue(any("已生效（解析失败重试，临时启用）" in line for line in logs.output))
                else:
                    # prefill 对 tool 没有可改的内容，维持原来的「保留上下文重试」
                    self.assertTrue(any("保留上下文重试 1/3" in line for line in logs.output))

    async def test_tool_errors_inside_a_batch_reach_recent_errors(self):
        engine = translator(*TOOL_ENGINES[0])
        engine.multi_turn = False
        replies = ["refusal", "bad-patch", "good"]

        async def model(**kwargs):
            rows = tool_input_rows(kwargs["messages"][1]["content"])
            reply = replies.pop(0)
            if reply == "refusal":
                kwargs["tool_response_holder"].update(tool_calls=[], finish_reason="stop")
                return "The prompt could not be submitted.", SimpleNamespace(model_name="m")
            line = "translated" if reply == "bad-patch" else "+translated"
            body = "\n".join(f"@@ {sig}|{obj['id']}\n{line}" for sig, obj in rows)
            kwargs["tool_response_holder"].update(
                tool_calls=[function_call("write_translation_result", f"*** Begin Patch\n{body}\n*** End Patch")],
                finish_reason="tool_calls",
            )
            return "", SimpleNamespace(model_name="m")

        engine.ask_chatbot = model
        recorded = []
        with patch("GalTransl.server.record_runtime_error", lambda *a, **kw: recorded.append(kw)), \
                redirect_stdout(io.StringIO()):
            count, _ = await engine.translate(sentences(1), filename="file")
        self.assertEqual(count, 1)
        self.assertEqual([(r["kind"], r["level"], r["filename"], r["index_range"], r["retry_count"]) for r in recorded],
                         [("parse", "warning", "file", "1", 1), ("parse", "warning", "file", "1", 2)])
        self.assertIn("缺少工具调用", recorded[0]["message"])
        self.assertIn("The prompt could not be submitted.", recorded[0]["message"])
        self.assertIn("补丁应用失败：译文行必须以 + 开头", recorded[1]["message"])

    async def test_tool_errors_are_not_double_recorded_in_auto_mode(self):
        engine = translator(*TOOL_ENGINES[0])
        engine.multi_turn = False
        engine.fail_fast = True

        async def model(**kwargs):
            kwargs["tool_response_holder"].update(tool_calls=[], finish_reason="stop")
            return "refused", SimpleNamespace(model_name="m")

        engine.ask_chatbot = model
        recorded = []
        with patch("GalTransl.server.record_runtime_error", lambda *a, **kw: recorded.append(kw)), \
                redirect_stdout(io.StringIO()), self.assertRaises(Exception):
            await engine.translate(sentences(1), filename="file")
        self.assertEqual(recorded, [])  # auto 自己会记录这次失败

    async def test_tool_template_prefill_mode_adds_nothing(self):
        engine = translator(*TOOL_ENGINES[0])
        engine.multi_turn = False
        engine.enhance_jailbreak = True
        requests = []

        async def model(**kwargs):
            requests.append(copy.deepcopy(kwargs["messages"]))
            rows = tool_input_rows(kwargs["messages"][1]["content"])
            body = "\n".join(f"@@ {sig}|{obj['id']}\n+t" for sig, obj in rows)
            kwargs["tool_response_holder"].update(
                tool_calls=[function_call("write_translation_result", f"*** Begin Patch\n{body}\n*** End Patch")],
                finish_reason="tool_calls",
            )
            return "", SimpleNamespace(model_name="test")

        engine.ask_chatbot = model
        with redirect_stdout(io.StringIO()):
            count, _ = await engine.translate(sentences(1), filename="part")
        self.assertEqual(count, 1)
        self.assertEqual([m["role"] for m in requests[0]], ["system", "user"])
        self.assertIsInstance(requests[0][1]["content"], str)

    def test_configuration(self):
        cases = (
            ({}, False, "prefill"),
            ({"gpt.enhance_jailbreak": "true", "gpt.enhance_jailbreak.mode": "IMAGE"}, True, "image"),
            ({"gpt.enhance_jailbreak": True, "gpt.enhance_jailbreak.mode": "bogus"}, True, "prefill"),
            ({"gpt.enhance_jailbreak": "false"}, False, "prefill"),
        )
        for settings, enabled, mode in cases:
            for kind, _ in ENGINES + TOOL_ENGINES:
                with self.subTest(engine=kind.__name__, settings=settings):
                    config = SimpleNamespace(getKey=lambda key, default=None: settings.get(key, default))
                    with (
                        patch.object(BaseTranslate, "__init__", return_value=None),
                        patch.object(kind, "init_chatbot"),
                        patch.object(kind, "_set_temp_type"),
                        patch.object(kind, "_apply_internal_prompt_template_overrides"),
                    ):
                        engine = kind(config, "", None, None)
                    self.assertEqual(engine.enhance_jailbreak, enabled)
                    self.assertEqual(engine.enhance_jailbreak_mode, mode)

    def test_nested_common_config_is_flattened_and_flat_keys_win(self):
        from GalTransl.ConfigHelper import flatten_nested_common

        # 旧版桌面端把新配置项存成了嵌套
        common = {"gpt.enhance_jailbreak": True, "gpt": {"enhance_jailbreak": {"mode": "image"}}}
        self.assertEqual(flatten_nested_common(common), {"gpt.enhance_jailbreak.mode": "image"})
        key_values = dict(common)
        for key, value in flatten_nested_common({**common, "gpt.enhance_jailbreak.mode": "prefill"}).items():
            key_values.setdefault(key, value)
        self.assertEqual(key_values["gpt.enhance_jailbreak.mode"], "image")
        key_values = {"gpt.enhance_jailbreak.mode": "prefill"}
        for key, value in flatten_nested_common(common).items():
            key_values.setdefault(key, value)
        self.assertEqual(key_values["gpt.enhance_jailbreak.mode"], "prefill")

    def test_render_produces_png_pages(self):
        urls = TextImage.render_text_data_urls("<input>\nabc|{\"id\": 1, \"src\": \"テスト、测试\"}\n</input>")
        self.assertEqual(len(urls), 1)
        header, data = urls[0].split(",", 1)
        self.assertEqual(header, "data:image/png;base64")
        self.assertTrue(base64.b64decode(data).startswith(b"\x89PNG"))
        long_text = "\n".join("行" * 200 for _ in range(80))
        self.assertGreater(len(TextImage.render_text_pages(long_text)), 1)

    def test_wrap_keeps_br_marker_intact(self):
        font = TextImage._load_font("")
        width = TextImage.PAGE_WIDTH - 2 * TextImage.PADDING
        for prefix in range(60):
            text = "あ" * prefix + "<br>" + "い" * 60
            parts = TextImage._wrap_line(text, font, width)
            self.assertEqual("".join(parts), text)
            self.assertEqual(sum(part.count("<br>") for part in parts), 1, prefix)


if __name__ == "__main__":
    unittest.main()
