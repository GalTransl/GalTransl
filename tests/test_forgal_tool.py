import copy
import io
import json
import re
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from openai.types.chat import ChatCompletion, ChatCompletionChunk
from unittest.mock import AsyncMock

from GalTransl.Backend.BaseTranslate import BaseTranslate
from GalTransl.Backend.ForGalToolTranslate import (
    ForGalToolTranslate, TRANSLATION_TOOLS, apply_translation_patch,
)
from GalTransl.Backend.Prompts import FORGAL_TOOL_TRANS_PROMPT
from test_translation_multiturn import sentences, translator
from test_translate_stream_progress import _make_engine, _StreamResponse


def tool_input_rows(prompt):
    from GalTransl.Backend.ForGalMarkdownTranslate import parse_markdown_row
    rows = []
    for line in prompt.split("<input>")[-1].split("</input>")[0].splitlines():
        cells = parse_markdown_row(line)
        if cells and re.fullmatch(r"[a-z0-9]{3}\|[0-9]+", cells[0]):
            sig, index = cells[0].split("|")
            rows.append((sig, {"id": int(index), "src": cells[-1]}))
    return rows


def function_call(name, text, call_id="c1"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps({"patch": text}, ensure_ascii=False),
    }}


def patch_for(key, text):
    return f"*** Begin Patch\n@@ {key}\n+{text}\n*** End Patch"


class PatchTests(unittest.TestCase):
    def test_patch_accepts_one_through_batch_size_and_rejects_extra_sentence(self):
        expected = {f"a{i:02}|{i}": "source" for i in range(24)}
        for count in (1, 16, 24):
            patch = "*** Begin Patch\n" + "\n".join(f"@@ {key}\n+translated" for key in list(expected)[:count]) + "\n*** End Patch"
            results = {}
            self.assertEqual(apply_translation_patch(patch, expected, results), count)
        patch = patch.replace("*** End Patch", "@@ a24|24\n+extra\n*** End Patch")
        results = {}
        with self.assertRaisesRegex(ValueError, "未知的 anchor"):
            apply_translation_patch(patch, expected, results)
        self.assertEqual(results, {})

    def test_patch_is_atomic_and_can_correct_previous_result(self):
        expected, results = {"abc|1": "source", "def|2": "source"}, {"abc|1": "old"}
        bad = "*** Begin Patch\n@@ abc|1\n+new\n@@ xxx|2\n+bad\n*** End Patch"
        with self.assertRaises(ValueError):
            apply_translation_patch(bad, expected, results)
        self.assertEqual(results, {"abc|1": "old"})
        self.assertEqual(apply_translation_patch(patch_for("abc|1", "new"), expected, results), 1)
        self.assertEqual(results, {"abc|1": "new"})

    def test_invalid_patches_do_not_write(self):
        for patch in ("{}", patch_for("abc|1", ""), patch_for("abc|1", "�"),
                      "*** Begin Patch\n@@ abc|1\n+ok\n@@ abc|1\n+duplicate\n*** End Patch"):
            results = {}
            with self.assertRaises(ValueError):
                apply_translation_patch(patch, {"abc|1": "source"}, results)
            self.assertEqual(results, {})

    def test_short_patch_multiple_entries_and_reject_legacy_format(self):
        expected = {"abc|1": "source", "def|2": "source"}
        short = "*** Begin Patch\n@@ abc|1\n+first<br>line\n@@ def|2\n+second\n*** End Patch"
        results = {}
        self.assertEqual(apply_translation_patch(short, expected, results), 2)
        self.assertEqual(results, {"abc|1": "first<br>line", "def|2": "second"})
        results = {}
        with self.assertRaises(ValueError):
            apply_translation_patch(short.replace("@@ ", "*** Update Translation: "), expected, results)
        self.assertEqual(results, {})


class ToolTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_function_calls_with_no_content_stream_and_nonstream(self):
        call = function_call("write_translation_result", "译文补丁")
        response = ChatCompletion.model_validate({
            "id": "r", "created": 0, "model": "demo", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [call],
            }}],
        })
        chunks = [ChatCompletionChunk.model_validate({
            "id": "r", "created": 0, "model": "demo", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "finish_reason": finish, "delta": {"tool_calls": [delta]}}],
        }) for delta, finish in [
            ({"index": 0, "id": "c1", "type": "function", "function": {"name": "write_translation_", "arguments": '{"patch": "译文'}}, None),
            ({"index": 0, "function": {"name": "result", "arguments": '补丁"}'}}, "tool_calls"),
        ]]
        for stream, value in [(False, response), (True, _StreamResponse([(0, c) for c in chunks]))]:
            engine = _make_engine([value], stream=stream)
            create = engine.client_list[0][0].chat.completions.create
            checked_create = AsyncMock(wraps=create)
            engine.client_list[0][0].chat.completions.create = checked_create
            holder = {}
            forced_choice = {"type": "function", "function": {"name": "write_translation_result"}}
            content, _ = await BaseTranslate.ask_chatbot(
                engine, messages=[], tools=TRANSLATION_TOOLS, tool_response_holder=holder,
                **({"tool_choice": forced_choice} if stream else {}),
            )
            self.assertFalse(content)
            self.assertEqual(holder, {"tool_calls": [call], "finish_reason": "tool_calls"})
            request = checked_create.call_args.kwargs
            self.assertEqual(request["tool_choice"], forced_choice if stream else "auto")
            self.assertEqual([tool["type"] for tool in request["tools"]], ["function"])
            for tool in request["tools"]:
                schema = tool["function"]["parameters"]
                self.assertEqual(schema["type"], "object")
                self.assertEqual(schema["properties"][schema["required"][0]]["type"], "string")


class TranslationToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_stubs_and_reworded_first_group_cannot_loop_forever(self):
        engine = translator(ForGalToolTranslate, FORGAL_TOOL_TRANS_PROMPT)
        engine.multi_turn = False
        starts, requests = [], []

        async def model(**kwargs):
            messages = kwargs["messages"]
            requests.append(copy.deepcopy(messages))
            rows = tool_input_rows(messages[1]["content"])
            # 每轮都先返回一个空参数调用，再返回无名称的实际 patch，复现日志。
            step = sum(m["role"] == "tool" for m in messages) // 2
            first_batch = rows[0][1]["id"] == 1
            if step == 0:
                starts.append(rows[0][1]["id"])
            group = rows[:16] if first_batch else rows[step * 16:(step + 1) * 16]
            calls = []
            if group:
                stub = function_call("write_translation_result", "unused", f"stub-{step}")
                stub["function"]["arguments"] = "{}"
                suffix = f"-revision-{step}" if first_batch else ""
                body = "\n".join(f"@@ {sig}|{obj['id']}\n+translated-{obj['id']}{suffix}" for sig, obj in group)
                calls = [stub, function_call("", f"*** Begin Patch\n{body}\n*** End Patch", f"patch-{step}")]
            kwargs["tool_response_holder"].update(tool_calls=calls, finish_reason="tool_calls" if calls else "stop")
            return "" if calls else "已完成翻译", SimpleNamespace(model_name="test")

        engine.ask_chatbot = model
        items = sentences(64)
        with redirect_stdout(io.StringIO()):
            result = await engine.batch_translate("file", "unused", items, 64, translist_unhit=items)
        self.assertEqual(starts, [1, 17])
        self.assertEqual(len(requests), 6)
        self.assertEqual([item.pre_dst for item in result[:16]], [f"translated-{i}-revision-2" for i in range(1, 17)])
        self.assertEqual([item.pre_dst for item in result[16:]], [f"translated-{i}" for i in range(17, 65)])
        self.assertIn("新增 0，修正 16", requests[2][-1]["content"])
        self.assertIn("Next patch anchors", requests[2][-1]["content"])

    async def test_repeated_first_group_preserves_progress_and_resumes_at_17(self):
        engine = translator(ForGalToolTranslate, FORGAL_TOOL_TRANS_PROMPT)
        engine.multi_turn = False
        starts, requests, published = [], [], []
        engine._record_runtime_success = lambda filename, tran: published.append(tran.index)

        async def model(**kwargs):
            messages = kwargs["messages"]
            requests.append(copy.deepcopy(messages))
            rows = tool_input_rows(messages[1]["content"])
            step = sum(m["role"] == "tool" for m in messages)
            if step == 0:
                starts.append(rows[0][1]["id"])
            # 首批一直重发 1-16；恢复后的请求正常处理 17-64。
            group = rows[:16] if rows[0][1]["id"] == 1 else rows[step * 16:(step + 1) * 16]
            calls = []
            if group:
                body = "\n".join(f"@@ {sig}|{obj['id']}\n+translated-{obj['id']}" for sig, obj in group)
                calls = [function_call("write_translation_result", f"*** Begin Patch\n{body}\n*** End Patch")]
            kwargs["tool_response_holder"].update(tool_calls=calls, finish_reason="tool_calls" if calls else "stop")
            return "" if calls else "已完成翻译", SimpleNamespace(model_name="test")

        engine.ask_chatbot = model
        items = sentences(64)
        with redirect_stdout(io.StringIO()):
            result = await engine.batch_translate("file", "unused", items, 64, translist_unhit=items)
        self.assertEqual(starts, [1, 17])
        self.assertEqual(len(requests), 7)
        self.assertEqual(published, list(range(1, 65)))
        self.assertEqual([item.pre_dst for item in result], [f"translated-{i}" for i in range(1, 65)])
        next_rows = tool_input_rows(requests[0][1]["content"])[16:]
        next_keys = ", ".join(f"{sig}|{obj['id']}" for sig, obj in next_rows)
        self.assertIn(f"Next patch anchors (in order): {next_keys}.", requests[1][-1]["content"])
        self.assertIn("16/64", requests[2][-1]["content"])
        self.assertIn("补丁重复提交已完成译文", requests[2][-1]["content"])
        self.assertIn(next_keys, requests[2][-1]["content"])

    async def test_partial_results_do_not_skip_an_unwritten_prefix(self):
        engine = translator(ForGalToolTranslate, FORGAL_TOOL_TRANS_PROMPT)
        calls_made = 0

        async def model(**kwargs):
            nonlocal calls_made
            calls_made += 1
            kwargs["tool_response_holder"].update(
                tool_calls=[function_call("write_translation_result", patch_for("def|2", "second"))],
                finish_reason="tool_calls",
            )
            return "", SimpleNamespace(model_name="test")

        engine.ask_chatbot = model
        with redirect_stdout(io.StringIO()):
            result, _ = await engine._ask_translation_batch(
                None, trans_list=sentences(2), sig_list=["abc", "def"], proofread=False,
                messages=[{"role": "user", "content": "input"}],
            )
        self.assertEqual(calls_made, 4)
        self.assertEqual(result, "")

    async def test_empty_stub_then_nameless_patch_stream_and_nonstream(self):
        stub = function_call("write_translation_result", "unused", "stub-id")
        stub["function"]["arguments"] = "{}"
        valid = function_call("", patch_for("abc|1", "translated"), "patch-id")
        envelope = {"id": "r", "created": 0, "model": "demo"}
        completion = ChatCompletion.model_validate({
            **envelope, "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [stub, valid],
            }}],
        })
        last_patch = function_call("write_translation_result", patch_for("def|2", "second"), "last-patch")
        final = ChatCompletion.model_validate({
            **envelope, "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [last_patch],
            }}],
        })
        # 两个独立 index/ID；第二个调用的参数跨 chunk，且没有名称。
        arguments = valid["function"]["arguments"]
        deltas = [
            {"tool_calls": [{"index": 0, **stub}]},
            {"tool_calls": [{"index": 1, "id": "patch-id", "type": "function", "function": {"arguments": arguments[:20]}}]},
            {"tool_calls": [{"index": 1, "function": {"arguments": arguments[20:]}}]},
        ]
        chunks = [ChatCompletionChunk.model_validate({
            **envelope, "object": "chat.completion.chunk",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if i == 2 else None, "delta": delta}],
        }) for i, delta in enumerate(deltas)]
        final_chunk = ChatCompletionChunk.model_validate({
            **envelope, "object": "chat.completion.chunk",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "delta": {"tool_calls": [{"index": 0, **last_patch}]}}],
        })
        for stream in (False, True):
            with self.subTest(stream=stream):
                engine = translator(ForGalToolTranslate, FORGAL_TOOL_TRANS_PROMPT)
                wire = _make_engine(
                    [_StreamResponse([(0, c) for c in chunks]), _StreamResponse([(0, final_chunk)])]
                    if stream else [completion, final], stream=stream,
                )
                requests, published = [], []
                engine._record_runtime_success = lambda filename, tran: published.append(tran.pre_dst)

                async def ask(**kwargs):
                    requests.append(copy.deepcopy(kwargs["messages"]))
                    return await BaseTranslate.ask_chatbot(wire, **kwargs)

                engine.ask_chatbot = ask
                with redirect_stdout(io.StringIO()):
                    result, _ = await engine._ask_translation_batch(
                        None, trans_list=sentences(2), sig_list=["abc", "def"], proofread=False,
                        messages=[{"role": "user", "content": "input"}],
                    )
                self.assertIn('"dst": "translated"', result)
                self.assertEqual(published, ["translated", "second"])
                self.assertEqual(len(requests), 2)
                assistant, failed, succeeded = requests[1][-3:]
                self.assertEqual([call["function"]["name"] for call in assistant["tool_calls"]], ["write_translation_result"] * 2)
                self.assertEqual(failed["tool_call_id"], "stub-id")
                self.assertIn("失败", failed["content"])
                self.assertEqual(succeeded["tool_call_id"], "patch-id")
                self.assertIn("成功", succeeded["content"])
                self.assertEqual(valid["function"]["name"], "")

    async def test_explicit_unknown_tool_name_is_not_replaced(self):
        engine = translator(ForGalToolTranslate, FORGAL_TOOL_TRANS_PROMPT)
        requests = []

        async def ask(**kwargs):
            requests.append(copy.deepcopy(kwargs["messages"]))
            calls = [function_call("unknown_tool", patch_for("abc|1", "invalid"))] if len(requests) == 1 else []
            kwargs["tool_response_holder"].update(tool_calls=calls, finish_reason="stop")
            return "", SimpleNamespace(model_name="test")

        engine.ask_chatbot = ask
        with redirect_stdout(io.StringIO()):
            result, _ = await engine._ask_translation_batch(
                None, trans_list=sentences(1), sig_list=["abc"], proofread=False,
                messages=[{"role": "user", "content": "input"}],
            )
        self.assertEqual(result, "")
        self.assertIn("未知工具：unknown_tool", requests[1][-1]["content"])

    async def test_text_between_patches_keeps_context_and_requests_patch_again(self):
        engine = translator(ForGalToolTranslate, FORGAL_TOOL_TRANS_PROMPT)
        requests, choices = [], []
        script = [
            ("", [function_call("write_translation_result", patch_for("abc|1", "first"))]),
            ("All looks good. Ready to patch.", []),
            ("", [function_call("write_translation_result", patch_for("def|2", "translated"))]),
        ]

        async def model(**kwargs):
            requests.append(copy.deepcopy(kwargs["messages"]))
            choices.append(kwargs["tool_choice"])
            content, calls = script.pop(0)
            kwargs["tool_response_holder"].update(tool_calls=calls, finish_reason="tool_calls" if calls else "stop")
            kwargs["reasoning_holder"].update(field="reasoning_content", text="thinking")
            return content, SimpleNamespace(model_name="test")

        engine.ask_chatbot = model
        with redirect_stdout(io.StringIO()):
            result, _ = await engine._ask_translation_batch(
                None, trans_list=sentences(2), sig_list=["abc", "def"], proofread=False,
                messages=[{"role": "user", "content": "original input"}], progress_file="file",
            )
        self.assertEqual(choices, [
            {"type": "function", "function": {"name": "write_translation_result"}},
            {"type": "function", "function": {"name": "write_translation_result"}},
            {"type": "function", "function": {"name": "write_translation_result"}},
        ])
        self.assertEqual(requests[2][:len(requests[1])], requests[1])
        self.assertIn("write_translation_result", requests[2][-1]["content"])
        self.assertEqual(requests[2][-2]["reasoning_content"], "thinking")
        self.assertIn('"dst": "translated"', result)
        self.assertEqual(script, [])

    async def test_repeated_patch_is_not_republished_but_correction_is(self):
        engine = translator(ForGalToolTranslate, FORGAL_TOOL_TRANS_PROMPT)
        published = []
        engine._record_runtime_success = lambda filename, tran: published.append((tran.index, tran.pre_dst))
        script = [
            function_call("write_translation_result", patch_for("abc|1", "first")),
            function_call("write_translation_result", patch_for("abc|1", "first")),
            function_call("write_translation_result", "*** Begin Patch\n@@ abc|1\n+corrected\n@@ def|2\n+second\n*** End Patch"),
        ]

        async def model(**kwargs):
            calls = [script.pop(0)] if script else []
            kwargs["tool_response_holder"].update(tool_calls=calls, finish_reason="tool_calls" if calls else "stop")
            return "" if calls else "已完成翻译", SimpleNamespace(model_name="test")

        engine.ask_chatbot = model
        with redirect_stdout(io.StringIO()):
            await engine._ask_translation_batch(
                None, trans_list=sentences(2), sig_list=["abc", "def"], proofread=False,
                messages=[{"role": "user", "content": "input"}], progress_file="file",
            )
        self.assertEqual(published, [(1, "first"), (1, "corrected"), (2, "second")])

    async def test_configured_batch_size_accepts_full_batch_patch_and_remainder(self):
        engine = translator(ForGalToolTranslate, FORGAL_TOOL_TRANS_PROMPT)
        engine.multi_turn = False
        engine.dynamic_num_per_request = True
        batch_sizes, group_sizes, called_tools = [], [], []
        published = []
        engine._record_runtime_success = lambda filename, tran: published.append(
            (filename, tran.index, tran.pre_dst, tran.trans_by)
        )

        async def model(**kwargs):
            messages = kwargs["messages"]
            rows = tool_input_rows(messages[1]["content"])
            step = sum(m["role"] == "tool" for m in messages)
            if step == 0:
                batch_sizes.append(len(rows))
            # 在下一次模型请求开始前，上一份成功 patch 已经发布；无需等待最终回复。
            self.assertEqual(len(published), sum(batch_sizes[:-1]) + min(len(rows), step * 24))
            group = rows[step * 24 : (step + 1) * 24]
            self.assertTrue(group, "不应在本批全部写入后额外请求结尾")
            self.assertEqual(kwargs["tool_choice"], {"type": "function", "function": {"name": "write_translation_result"}})
            calls = []
            if group:
                group_sizes.append(len(group))
                body = "\n".join(f"@@ {sig}|{obj['id']}\n+translated-{obj['id']}" for sig, obj in group)
                call = function_call("write_translation_result", f"*** Begin Patch\n{body}\n*** End Patch")
                calls = [call]
                called_tools.append(call["function"]["name"])
            kwargs["tool_response_holder"].update(tool_calls=calls, finish_reason="tool_calls" if calls else "stop")
            return "" if calls else "已完成翻译", SimpleNamespace(model_name="test")

        engine.ask_chatbot = model
        items = sentences(49)
        with redirect_stdout(io.StringIO()):
            result = await engine.batch_translate("file", "unused", items, 24, translist_unhit=items)
        self.assertEqual(batch_sizes, [24, 24, 1])
        self.assertEqual(group_sizes, [24, 24, 1])
        self.assertEqual(called_tools, ["write_translation_result"] * 3)
        self.assertEqual([item.pre_dst for item in result], [f"translated-{i}" for i in range(1, 50)])
        self.assertEqual(published, [("file", i, f"translated-{i}", "test") for i in range(1, 50)])

    async def test_invalid_function_arguments_return_paired_errors(self):
        for arguments in ('{"patch":', '[]', 'null', '{"patch": 123}', '{}'):
            engine = translator(ForGalToolTranslate, FORGAL_TOOL_TRANS_PROMPT)
            requests = []

            async def model(**kwargs):
                requests.append(copy.deepcopy(kwargs["messages"]))
                call = function_call("write_translation_result", "unused", "bad-args")
                call["function"]["arguments"] = arguments
                kwargs["tool_response_holder"].update(
                    tool_calls=[call] if len(requests) == 1 else [], finish_reason="stop",
                )
                return "", SimpleNamespace(model_name="test")

            engine.ask_chatbot = model
            with redirect_stdout(io.StringIO()):
                result, _ = await engine._ask_translation_batch(
                    None, trans_list=sentences(1), sig_list=["abc"], proofread=False,
                    messages=[{"role": "user", "content": "input"}],
                )
            self.assertEqual(result, "")
            self.assertEqual(requests[1][-1]["role"], "tool")
            self.assertEqual(requests[1][-1]["tool_call_id"], "bad-args")
            self.assertIn("失败", requests[1][-1]["content"])

    async def test_patch_retry_reasoning_and_independent_batches(self):
        engine = translator(ForGalToolTranslate, FORGAL_TOOL_TRANS_PROMPT)
        engine.multi_turn = False
        engine._last_chatbot_was_stream = True
        requests = []
        round_index = 0
        published = []
        engine._record_runtime_success = lambda filename, tran: published.append(
            (filename, tran.index, tran.pre_dst, tran.trans_by)
        )

        async def model(**kwargs):
            nonlocal round_index
            # 无效 patch 不发布译文；成功 patch 后直接进入下一批。
            self.assertEqual(len(published), round_index // 2)
            messages = kwargs["messages"]
            requests.append(copy.deepcopy(messages))
            sig, obj = tool_input_rows(messages[1]["content"])[0]
            index = obj["id"]
            patch = patch_for(f"{sig}|{index}", "translated<br>text[t]end")
            scripted = [
                [function_call("write_translation_result", patch_for("xxx|999", "wrong"))],
                [function_call("write_translation_result", patch)],
            ]
            calls = scripted[round_index % 2]
            round_index += 1
            kwargs["tool_response_holder"].update(tool_calls=calls, finish_reason="tool_calls" if calls else "stop")
            kwargs["reasoning_holder"].update(field="reasoning_content", text="thinking")
            return ("" if calls else "已完成翻译"), SimpleNamespace(model_name="test-model")

        engine.ask_chatbot = model
        items = sentences(2)
        console = io.StringIO()
        for item in items:
            item.post_src = "source\ntext\tend"
            with redirect_stdout(console):
                count, result = await engine.translate([item], filename="file")
            self.assertEqual(count, 1)
            self.assertEqual(result[0].pre_dst, "translated\ntext\tend")
        self.assertEqual(len(requests), 4)
        self.assertEqual(len(requests[0]), 2)
        self.assertEqual(len(requests[2]), 2)
        self.assertIn("失败", requests[1][-1]["content"])
        self.assertEqual(requests[1][-2]["reasoning_content"], "thinking")
        self.assertEqual(published, [("file", i, "translated\ntext\tend", "test-model") for i in (1, 2)])
        logs = console.getvalue()
        self.assertIn("[write_translation_result] 工具输入：\n*** Begin Patch", logs)
        self.assertIn("[write_translation_result] 工具返回：\n补丁应用成功", logs)
        self.assertIn("[write_translation_result] 工具返回：\n补丁应用失败", logs)
        self.assertIn("All sentences are written.", logs)

    async def test_truncated_calls_and_text_only_are_not_results(self):
        engine = translator(ForGalToolTranslate, FORGAL_TOOL_TRANS_PROMPT)
        for finish, calls in [("length", [function_call("write_translation_result", patch_for("abc|1", "truncated"))]), ("stop", [])]:
            async def model(**kwargs):
                kwargs["tool_response_holder"].update(tool_calls=calls, finish_reason=finish)
                return 'abc|{"id":1,"dst":"untrusted"}', SimpleNamespace(model_name="test")
            engine.ask_chatbot = model
            result, _ = await engine._ask_translation_batch(
                None, trans_list=sentences(1), sig_list=["abc"], proofread=False,
                messages=[{"role": "user", "content": "input"}],
            )
            self.assertEqual(result, "")
