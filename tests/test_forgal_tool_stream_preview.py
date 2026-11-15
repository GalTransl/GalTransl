"""tool 模板的提前显示：补丁还在流式接收时，写完的句子每攒够 8 句就先推到「最近译文」。

回归背景：tool 模板要等整份补丁收完、校验通过才上报译文；单次句数调大后（32 句一批）
工作台要等很久才一次性出现一整批。这里用真实的流式 chunk 走 BaseTranslate.ask_chatbot，
守住三点：
- 推送发生在响应收完之前，按 8 句一组；
- 每句只上报一次（实时速度按上报次数算，重复上报会虚高）；
- 这只是显示：补丁最后没通过校验时，句子不会因为显示过就被采用。
"""

import io
import json
import unittest
from contextlib import redirect_stdout

from openai.types.chat import ChatCompletionChunk

from GalTransl.Backend.BaseTranslate import BaseTranslate
from GalTransl.Backend.ForGalToolTranslate import (
    STREAM_PREVIEW_SENTENCES, ForGalToolTranslate, streamed_patch_entries,
)
from GalTransl.Backend.Prompts import FORGAL_TOOL_TRANS_PROMPT
from test_translate_stream_progress import _make_engine, _StreamResponse
from test_translation_multiturn import sentences, translator


def _patch(entries):
    body = "".join(f"@@ {key}\n+{text}\n" for key, text in entries)
    return f"*** Begin Patch\n{body}*** End Patch"


def _stream(patch_text, call_id="c1", step=7):
    """把一次工具调用切成很多个小 chunk，模拟模型逐 token 输出参数。"""
    arguments = json.dumps({"patch": patch_text}, ensure_ascii=False)
    pieces = [arguments[i:i + step] for i in range(0, len(arguments), step)]
    chunks = []
    for i, piece in enumerate(pieces):
        call = {"index": 0, "function": {"arguments": piece}}
        if i == 0:
            call.update(id=call_id, type="function")
            call["function"]["name"] = "write_translation_result"
        chunks.append(ChatCompletionChunk.model_validate({
            "id": "r", "created": 0, "model": "demo", "object": "chat.completion.chunk",
            "choices": [{
                "index": 0, "delta": {"tool_calls": [call]},
                "finish_reason": "tool_calls" if i == len(pieces) - 1 else None,
            }],
        }))
    return _StreamResponse([(0, chunk) for chunk in chunks])


class StreamedPatchEntriesTests(unittest.TestCase):
    def test_only_entries_followed_by_another_header_are_complete(self):
        arguments = json.dumps({"patch": _patch([("abc|1", "一"), ("def|2", "二\n+第二行"), ("ghi|3", "三")])}, ensure_ascii=False)
        # 收完整份时最后一条后面没有新的 @@，仍不算写完：它由收尾的整份校验处理
        self.assertEqual(streamed_patch_entries(arguments), [("abc|1", "一"), ("def|2", "二\n第二行")])
        cut = arguments.index("ghi") - 6  # 第三条的 @@ 还没出现
        self.assertEqual(streamed_patch_entries(arguments[:cut]), [("abc|1", "一")])

    def test_unrecognised_or_unfinished_arguments_yield_nothing(self):
        for arguments in ("", "{", '{"patch":"*** Begin Patch\\n@@ abc|1\\n+一', '{"patch":"oops\\nnot a patch\\n@@ a', '{"other":"x\\n@@ a'):
            self.assertEqual(streamed_patch_entries(arguments), [], arguments)


class ToolStreamPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, rows, sigs, responses):
        engine = translator(ForGalToolTranslate, FORGAL_TOOL_TRANS_PROMPT)
        wire = _make_engine(responses, stream=True)
        events = []
        engine._record_runtime_success = lambda filename, tran: events.append(tran.index)

        async def ask(**kwargs):
            result = await BaseTranslate.ask_chatbot(wire, **kwargs)
            events.append("response-done")
            return result

        engine.ask_chatbot = ask
        with redirect_stdout(io.StringIO()):
            result, _ = await engine._ask_translation_batch(
                None, trans_list=rows, sig_list=sigs, proofread=False,
                messages=[{"role": "user", "content": "input"}],
            )
        return result, events

    async def test_sentences_are_pushed_in_groups_before_the_response_finishes(self):
        rows = sentences(20)
        sigs = [f"s{i:02d}" for i in range(20)]
        entries = [(f"{sig}|{row.index}", f"译文{row.index}") for sig, row in zip(sigs, rows)]
        result, events = await self._run(rows, sigs, [_stream(_patch(entries))])

        done = events.index("response-done")
        self.assertEqual(STREAM_PREVIEW_SENTENCES, 8)
        # 收完之前已经推了两组 8 句；第 17~20 句等整份补丁通过后再出现
        self.assertEqual(events[:done], list(range(1, 17)))
        self.assertEqual(events[done + 1:], [17, 18, 19, 20])
        self.assertEqual(result.count('"dst"'), 20)

    async def test_small_batches_are_not_previewed(self):
        rows = sentences(4)
        sigs = ["aaa", "bbb", "ccc", "ddd"]
        entries = [(f"{sig}|{row.index}", f"译文{row.index}") for sig, row in zip(sigs, rows)]
        _result, events = await self._run(rows, sigs, [_stream(_patch(entries))])
        self.assertEqual(events, ["response-done", 1, 2, 3, 4])

    async def test_rejected_patch_is_not_adopted_and_retry_is_not_reported_twice(self):
        rows = sentences(10)
        sigs = [f"s{i:02d}" for i in range(10)]
        entries = [(f"{sig}|{row.index}", f"译文{row.index}") for sig, row in zip(sigs, rows)]
        # 第一份补丁最后混进一个不存在的锚点：整份作废，但前 8 句已经提前显示过
        bad = entries + [("zzz|99", "多出来的一句")]
        result, events = await self._run(rows, sigs, [_stream(_patch(bad)), _stream(_patch(entries), "c2")])

        first_done = events.index("response-done")
        self.assertEqual(events[:first_done], list(range(1, 9)))
        # 作废的补丁没有写入任何句子：结果全部来自第二份补丁
        self.assertEqual(result.count('"dst"'), 10)
        # 第二份里相同的译文不重复上报，只补上没显示过的第 9、10 句
        self.assertEqual([event for event in events if event != "response-done"], list(range(1, 11)))
        self.assertTrue(all(row.pre_dst == f"译文{row.index}" for row in rows))


if __name__ == "__main__":
    unittest.main()
